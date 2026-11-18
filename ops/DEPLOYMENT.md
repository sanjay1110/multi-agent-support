# Deployment & Operations

## 1. Run with Docker Compose (minimum deployment)

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY and a strong ADMIN_TOKEN
docker compose up --build -d    # API on http://localhost:8000
docker compose ps               # healthcheck should show "healthy"
docker compose logs -f api      # structured JSON logs
```

Optional:
```bash
docker compose --profile eval run --rm eval            # run the benchmark inside the image
docker compose --profile observability up -d           # Jaeger UI on http://localhost:16686
# then set OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 in .env and restart api
```

## 2. Secrets
| Secret | Source | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | env / `.env` (git-ignored) | In prod: AWS Secrets Manager / GCP Secret Manager / Vault, injected as env at runtime |
| `ADMIN_TOKEN` | env / `.env` | Generate with `python -c "import secrets;print(secrets.token_urlsafe(32))"`; rotate quarterly. Prod: replace with SSO/OIDC |

Secrets are never baked into the image, never logged (`Settings` uses `repr=False`), and `.env` is in `.gitignore`.

## 3. Container hardening (already in the compose file)
- Non-root user (uid 10001), `read_only: true` root filesystem, `no-new-privileges`, writes only to named volumes.
- `HEALTHCHECK` on `/healthz`; `/readyz` checks the backend for load-balancer readiness.

## 4. Observability
| Signal | Where | What |
|---|---|---|
| Structured logs | stdout + `ops/logs/app.jsonl` | One JSON object per event, always with `request_id` and `session_id` |
| Traces | OTLP → Jaeger (optional) + `span` log lines | Spans: `request` → `llm.call` (router) → `agent.<name>` → `llm.call` / `tool.<name>` |
| Per-request metrics | `request_complete` log line + API response `usage` | latency_ms, llm_calls, input/output tokens, tool_calls, cost_usd |
| Audit | `data/audit.jsonl` | Hash-chained security events; verify via `GET /v1/audit/verify` |

Useful queries (with `jq`):
```bash
# p95-ish latency and cost of last 200 requests
jq -s '[.[]|select(.msg=="request_complete")]|{n:length,avg_ms:(map(.latency_ms)|add/length),cost:(map(.usage_cost_usd)|add)}' ops/logs/app.jsonl
# everything that happened in one request
jq 'select(.request_id=="req_xxx")' ops/logs/app.jsonl
# guardrail blocks
jq 'select(.msg=="guardrail_block")' ops/logs/app.jsonl
```

### Suggested alerts
| Alert | Condition |
|---|---|
| Error rate | `outcome=error_handled` > 5% over 10 min |
| Latency | p95 `latency_ms` > 8 s over 10 min |
| Cost | hourly `usage_cost_usd` sum > budget |
| Attack spike | `guardrail_block` count > 20 / 5 min |
| Approval backlog | pending approvals older than 4 h |
| Audit integrity | `/v1/audit/verify` returns `chain_valid=false` (page immediately) |

## 5. Scaling
- **The API is stateless per request.** All state lives in the data layer, so you can scale horizontally behind a load balancer.
- The demo uses **SQLite** (one writer), so run 1 replica. For more than 1 replica:
  - Session and working memory → **PostgreSQL** or **Redis** (swap `memory/*.py` stores; same interface).
  - Approval queue → PostgreSQL table, or a queue such as SQS so approvers' UI and workers scale separately.
  - Audit log → append-only table or log pipeline (Kinesis/Kafka → S3 Object Lock).
- **LLM throughput:** the router uses a small, fast model; specialists use a larger one. Watch Anthropic rate limits; the SDK retries 429/5xx with backoff (`max_retries=3`). Add a per-customer rate limit at the gateway.
- **Cost controls:** `MAX_AGENT_STEPS`, `MAX_TOKENS`, history window (`SessionStore.max_history=12`), and slot-filling in the orchestrator (asks for an order id **before** calling a specialist).
- **Kubernetes path:** the same image runs as a Deployment + HPA on CPU/RPS; secrets via External Secrets; probes on `/healthz` and `/readyz`.

## 6. Release & rollback
1. CI (`.github/workflows/ci.yml`) runs unit tests, the **offline eval gate** (pass rate ≥ 95%), and builds the image.
2. Before promoting a **prompt or model change**, run the eval against Claude (`python -m eval.run_eval --mode anthropic --threshold 0.9`) and compare with the previous `eval/results/latest_anthropic.json`.
3. Tag images immutably: `IMAGE_TAG=$(git rev-parse --short HEAD) docker compose build`.
4. Deploy: `IMAGE_TAG=<new> docker compose up -d`. Watch error rate, latency and cost for 15 min (canary 10% first, if behind a load balancer).

**Rollback** (any alert, or eval regression):
```bash
IMAGE_TAG=<previous_sha> docker compose up -d     # code / prompt rollback (prompts ship in the image)
# model-only rollback without rebuild:
ANTHROPIC_MODEL=<previous_model> docker compose up -d
```
- Data migrations must be backward compatible (add columns, don't drop), so the previous image still runs against the new schema.
- Pending approvals survive rollbacks (persistent volume). Approved actions re-run business prechecks at execution time, so a rollback can't execute a now-invalid refund.
- Kill switch: set `LLM_MODE=mock` (deterministic, no external calls) or scale API to 0 and route the support widget to "contact us".

## 7. Runbook — common incidents
| Symptom | Check | Action |
|---|---|---|
| Many `BACKEND_UNAVAILABLE` | upstream service health | Customers already get a graceful retry message; escalate to the owning team |
| Spike in cost | `usage_llm_calls` per request | Look for step-limit hits; lower `MAX_AGENT_STEPS`; roll back the prompt |
| Wrong routing | eval route accuracy, `routed` logs | Adjust `ROUTER_PROMPT`, add the failing case to `eval/cases.jsonl` |
| Approval backlog | `GET /v1/approvals` | Page the support-lead rotation |
