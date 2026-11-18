"""Tool registry: binds typed specs to handlers and runs them safely.

Every tool has:
* a Pydantic input model (validated before any backend call),
* a Pydantic output model (only these fields reach the LLM),
* a permission ``scope`` (checked by security/permissions.py),
* an ``irreversible`` flag (routed through human approval),
* an optional ``precheck`` that validates business rules with no side effects.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel

from tools.backend import BackendError
from tools.schemas import ToolError, ToolErrorCode, ToolResult

Handler = Callable[[BaseModel], BaseModel]
Precheck = Callable[[BaseModel], None]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler
    scope: str
    irreversible: bool = False
    order_scoped: bool = True
    precheck: Optional[Precheck] = None
    max_retries: int = 1

    def anthropic_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas_for(self, names: list[str]) -> list[dict[str, Any]]:
        return [self._tools[n].anthropic_schema() for n in names if n in self._tools]

    # ------------------------------------------------------------------ run
    def run(self, spec: ToolSpec, inp: BaseModel) -> ToolResult:
        """Execute a validated input with retry on transient errors."""
        t0 = time.perf_counter()
        attempt = 0
        while True:
            try:
                out = spec.handler(inp)
                out = spec.output_model.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
                return ToolResult(tool=spec.name, ok=True, data=out.model_dump(),
                                  latency_ms=_ms(t0))
            except BackendError as e:
                if e.retryable and attempt < spec.max_retries:
                    attempt += 1
                    time.sleep(0.05 * attempt)          # small backoff
                    continue
                return ToolResult(tool=spec.name, ok=False, latency_ms=_ms(t0),
                                  error=ToolError(code=e.code, message=e.message, retryable=e.retryable))
            except Exception as e:  # noqa: BLE001 — never leak a stack trace to the LLM
                return ToolResult(tool=spec.name, ok=False, latency_ms=_ms(t0),
                                  error=ToolError(code=ToolErrorCode.INTERNAL,
                                                  message=f"Internal tool error ({type(e).__name__})."))

    def run_precheck(self, spec: ToolSpec, inp: BaseModel) -> Optional[ToolError]:
        if spec.precheck is None:
            return None
        try:
            spec.precheck(inp)
            return None
        except BackendError as e:
            return ToolError(code=e.code, message=e.message, retryable=e.retryable)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)
