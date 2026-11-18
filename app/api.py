"""HTTP API (FastAPI).

Customer endpoints : POST /v1/chat  (X-Access-Token, when ACCESS_TOKEN is set)
Approver endpoints : GET /v1/approvals, POST /v1/approvals/{id}/decision  (X-Admin-Token)
Ops endpoints      : GET /healthz, GET /readyz, GET /v1/audit/verify (X-Admin-Token)

Run: uvicorn app.api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings
from app.container import build_system
from memory.session_store import SessionOwnershipError
from ops.logging_config import setup_logging
from ops.tracing import setup_tracing

settings = Settings.from_env()
setup_logging(settings.log_level, settings.log_file)
setup_tracing()
system = build_system(settings)
log = logging.getLogger("api")
log.info("startup", extra={"llm_mode": system.llm.mode, "model": settings.model,
                           "chat_auth": bool(settings.access_token)})
if not settings.access_token:
    log.warning("ACCESS_TOKEN is not set: /v1/chat is open to anyone who can reach this server")

app = FastAPI(title="ShopCo Multi-Agent Support", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Strict policy for the chat page: only this origin's own script, styles and API.
# Not applied to /docs, which loads Swagger UI from a CDN.
_PAGE_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@app.middleware("http")
async def page_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers.update(_PAGE_HEADERS)
    return response


@app.get("/", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "chat.html", headers={"Cache-Control": "no-cache"})


class ChatIn(BaseModel):
    customer_id: str = Field(pattern=r"^C-\d{3}$",
                             description="In production this comes from the auth token, not the body.")
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{4,64}$")


class DecisionIn(BaseModel):
    approve: bool
    approver: str = Field(min_length=2, max_length=64)
    note: str = Field(default="", max_length=500)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_access(x_access_token: str = Header(default="")) -> None:
    """Gate customer endpoints behind a shared key when ACCESS_TOKEN is set.

    This only keeps strangers out; it does not identify the customer, so the
    body's customer_id is still trusted. Real per-customer auth is needed for production.
    """
    if not settings.access_token:
        return
    if not hmac.compare_digest(x_access_token.encode(), settings.access_token.encode()):
        raise HTTPException(status_code=401, detail="invalid access token")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    system.backend.get_order_owner("ORD-1001")
    return {"status": "ready", "llm_mode": system.llm.mode}


@app.post("/v1/chat", dependencies=[Depends(require_access)])
def chat(body: ChatIn) -> dict:
    try:
        resp = system.orchestrator.handle(body.message, body.customer_id, body.session_id)
    except SessionOwnershipError:
        raise HTTPException(status_code=403, detail="session does not belong to this customer")
    return resp.to_dict()


@app.get("/v1/approvals", dependencies=[Depends(require_admin)])
def list_approvals(status: Optional[str] = "pending") -> list[dict]:
    return system.approvals.list(status=status or None)


@app.post("/v1/approvals/{approval_id}/decision", dependencies=[Depends(require_admin)])
def decide(approval_id: str, body: DecisionIn) -> dict:
    try:
        return system.approvals.decide(approval_id, approve=body.approve, approver=body.approver,
                                       note=body.note)
    except KeyError:
        raise HTTPException(404, "approval not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/v1/sessions/{session_id}/memory", dependencies=[Depends(require_admin)])
def memory(session_id: str) -> dict:
    return {"working_memory": system.working.load(session_id).model_dump(),
            "history": system.sessions.history(session_id, limit=50)}


@app.get("/v1/audit/verify", dependencies=[Depends(require_admin)])
def audit_verify() -> dict:
    ok, n = system.audit.verify()
    return {"chain_valid": ok, "entries": n}
