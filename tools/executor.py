"""Secure tool executor — the single choke point between agents and backends.

Pipeline for every tool call requested by an LLM:

    1. permission boundary   agent allow-list          -> PERMISSION_DENIED
    2. input validation      Pydantic strict model     -> INVALID_INPUT
    3. resource ownership    order.customer == caller  -> NOT_OWNER
    4. irreversible?         business precheck, then queue for human approval
    5. execute               with retry on transient errors
    6. audit + trace         every outcome is recorded
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from ops.context import customer_id_var, current_metrics, request_id_var, session_id_var
from ops.tracing import span
from security import permissions
from security.approval import ApprovalQueue
from security.audit import AuditLog
from tools.backend import BackendError, SimulatedBackend
from tools.registry import ToolRegistry
from tools.schemas import ToolError, ToolErrorCode, ToolResult

log = logging.getLogger("tools.executor")


@dataclass
class ToolExecutor:
    registry: ToolRegistry
    backend: SimulatedBackend
    approvals: ApprovalQueue
    audit: AuditLog

    def execute(self, agent: str, tool: str, args: dict[str, Any]) -> ToolResult:
        customer_id = customer_id_var.get()
        m = current_metrics()
        if m is not None:
            m.tool_calls += 1
        with span(f"tool.{tool}", agent=agent, tool=tool) as sp:
            result = self._execute(agent, tool, args or {}, customer_id)
            sp["ok"] = result.ok
            if result.error:
                sp["error_code"] = result.error.code.value
            if result.approval_id:
                sp["approval_id"] = result.approval_id
        self.audit.record("tool_call", actor=agent, tool=tool, args=args, ok=result.ok,
                          error=result.error.code.value if result.error else None,
                          approval_id=result.approval_id, customer_id=customer_id)
        return result

    # ------------------------------------------------------------------ core
    def _execute(self, agent: str, tool: str, args: dict[str, Any],
                 customer_id: Optional[str]) -> ToolResult:
        # 1. permission boundary
        if not permissions.is_allowed(agent, tool):
            self.audit.record("permission_denied", actor=agent, tool=tool)
            return _err(tool, ToolErrorCode.PERMISSION_DENIED,
                        f"Agent '{agent}' is not permitted to call '{tool}'.")
        spec = self.registry.get(tool)
        if spec is None:
            return _err(tool, ToolErrorCode.INVALID_INPUT, f"Unknown tool '{tool}'.")

        # 2. input validation
        try:
            inp = spec.input_model.model_validate(args)
        except ValidationError as e:
            msgs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            return _err(tool, ToolErrorCode.INVALID_INPUT, f"Invalid arguments: {msgs}")

        # 3. ownership (customers may only touch their own orders)
        if spec.order_scoped:
            order_id = getattr(inp, "order_id")
            try:
                owner = self.backend.get_order_owner(order_id)
            except BackendError as e:
                return _err(tool, e.code, e.message, e.retryable)
            if customer_id and owner != customer_id:
                self.audit.record("ownership_violation", actor=agent, tool=tool,
                                  order_id=order_id, customer_id=customer_id)
                # Same message as NOT_FOUND would leak existence; be explicit but generic.
                return _err(tool, ToolErrorCode.NOT_OWNER,
                            f"Order {order_id} is not associated with this customer account.")

        # 4. irreversible -> human approval
        if spec.irreversible:
            pre = self.registry.run_precheck(spec, inp)
            if pre is not None:
                return ToolResult(tool=tool, ok=False, error=pre)
            approval_id = self.approvals.create(
                tool=tool, args=inp.model_dump(), agent=agent,
                session_id=session_id_var.get(), customer_id=customer_id,
                request_id=request_id_var.get(), summary=_summary(tool, inp.model_dump()))
            log.info("approval_created", extra={"approval_id": approval_id, "tool": tool})
            return ToolResult(tool=tool, ok=True, approval_id=approval_id, data={
                "status": "pending_human_approval",
                "approval_id": approval_id,
                "message": "Request recorded and queued for review by a support specialist. "
                           "It has NOT been executed yet.",
                **inp.model_dump(),
            })

        # 5. execute
        return self.registry.run(spec, inp)


def _err(tool: str, code: ToolErrorCode, msg: str, retryable: bool = False) -> ToolResult:
    return ToolResult(tool=tool, ok=False, error=ToolError(code=code, message=msg, retryable=retryable))


def _summary(tool: str, args: dict[str, Any]) -> str:
    if tool == "issue_refund":
        return f"Refund ${args['amount']:.2f} for {args['order_id']} — {args['reason']}"
    if tool == "file_insurance_claim":
        return f"Insurance claim ({args['incident_type']}) for {args['order_id']}"
    return f"{tool} {args}"
