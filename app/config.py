"""Runtime configuration — 12-factor style, everything from environment variables.

Secrets (ANTHROPIC_API_KEY, ADMIN_TOKEN, ACCESS_TOKEN) are only ever read from the
environment / .env and never logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no extra dependency). Real env vars take precedence."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    llm_mode: str = "auto"                 # auto | anthropic | mock
    anthropic_api_key: str = field(default="", repr=False)
    model: str = "claude-sonnet-5-5"
    router_model: str = "claude-haiku-5-5"
    max_tokens: int = 1024
    price_input_per_mtok: float = 3.0      # USD per 1M input tokens — set to your model's price
    price_output_per_mtok: float = 15.0
    router_price_input_per_mtok: float = 1.0
    router_price_output_per_mtok: float = 5.0
    data_dir: str = "data"
    log_level: str = "INFO"
    log_file: str = "ops/logs/app.jsonl"
    admin_token: str = field(default="change-me", repr=False)
    # Shared key callers must send as X-Access-Token to use /v1/chat. Empty = chat is open
    # (local dev only). Set it before exposing the API beyond localhost.
    access_token: str = field(default="", repr=False)
    sim_today: str = "2026-11-17"
    sim_latency_ms: int = 0
    max_agent_steps: int = 6

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        e = os.environ.get
        return cls(
            llm_mode=e("LLM_MODE", "auto").lower(),
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            model=e("ANTHROPIC_MODEL", cls.model),
            router_model=e("ANTHROPIC_ROUTER_MODEL", cls.router_model),
            max_tokens=int(e("MAX_TOKENS", cls.max_tokens)),
            price_input_per_mtok=float(e("PRICE_INPUT_PER_MTOK", cls.price_input_per_mtok)),
            price_output_per_mtok=float(e("PRICE_OUTPUT_PER_MTOK", cls.price_output_per_mtok)),
            router_price_input_per_mtok=float(e("ROUTER_PRICE_INPUT_PER_MTOK", cls.router_price_input_per_mtok)),
            router_price_output_per_mtok=float(e("ROUTER_PRICE_OUTPUT_PER_MTOK", cls.router_price_output_per_mtok)),
            data_dir=e("DATA_DIR", cls.data_dir),
            log_level=e("LOG_LEVEL", cls.log_level),
            log_file=e("LOG_FILE", cls.log_file),
            admin_token=e("ADMIN_TOKEN", cls.admin_token),
            access_token=e("ACCESS_TOKEN", cls.access_token),
            sim_today=e("SIM_TODAY", cls.sim_today),
            sim_latency_ms=int(e("SIM_LATENCY_MS", cls.sim_latency_ms)),
            max_agent_steps=int(e("MAX_AGENT_STEPS", cls.max_agent_steps)),
        )

    @property
    def resolved_llm_mode(self) -> str:
        if self.llm_mode == "auto":
            return "anthropic" if self.anthropic_api_key else "mock"
        return self.llm_mode
