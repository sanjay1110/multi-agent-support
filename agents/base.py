"""Specialist agent: a focused system prompt + a least-privilege tool set +
a bounded tool-use loop (Anthropic messages / tool_use protocol)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agents.llm import LLMClient
from ops.tracing import span
from security import permissions
from tools.executor import ToolExecutor
from tools.registry import ToolRegistry
from tools.schemas import ToolResult

log = logging.getLogger("agents")


@dataclass
class AgentResult:
    agent: str
    reply: str
    tool_results: list[ToolResult] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    hit_step_limit: bool = False


class SpecialistAgent:
    name: str = "base"
    system_prompt: str = ""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, executor: ToolExecutor,
                 max_steps: int = 6):
        self.llm = llm
        self.registry = registry
        self.executor = executor
        self.max_steps = max_steps
        # Least privilege: the model is only shown tools it is allowed to call.
        self.tool_names = permissions.allowed_tools(self.name)
        self.tool_schemas = registry.schemas_for(self.tool_names)

    def run(self, history: list[dict[str, Any]], task: str) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": task})
        result = AgentResult(agent=self.name, reply="")
        with span(f"agent.{self.name}", agent=self.name) as sp:
            for step in range(1, self.max_steps + 1):
                result.steps = step
                resp = self.llm.complete(agent=self.name, system=self.system_prompt,
                                         messages=messages, tools=self.tool_schemas)
                messages.append({"role": "assistant", "content": resp.content or
                                 [{"type": "text", "text": resp.text or "..."}]})
                if not resp.tool_calls:
                    result.reply = resp.text.strip()
                    break
                tool_blocks = []
                for call in resp.tool_calls:
                    tr = self.executor.execute(self.name, call.name, call.input)
                    result.tool_results.append(tr)
                    result.tool_calls.append({"tool": call.name, "input": call.input, "ok": tr.ok,
                                              "error": tr.error.code.value if tr.error else None,
                                              "approval_id": tr.approval_id})
                    tool_blocks.append({"type": "tool_result", "tool_use_id": call.id,
                                        "content": json.dumps(tr.for_llm(), default=str),
                                        "is_error": not tr.ok})
                messages.append({"role": "user", "content": tool_blocks})
            else:
                result.hit_step_limit = True
                result.reply = ("I'm sorry, I wasn't able to finish that request. I've noted it and a "
                                "specialist can follow up with you.")
            sp.update(steps=result.steps, tools=len(result.tool_calls))
        return result
