"""Tracing: OpenTelemetry spans (when configured) + a structured log per span.

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` set -> spans exported via OTLP (e.g. to Jaeger,
  see docker-compose ``observability`` profile).
* ``OTEL_CONSOLE=1`` -> spans printed to stdout.
* otherwise spans are still created (for trace ids) and every span is logged as
  JSON with its duration, so per-request traces exist in ``ops/logs/app.jsonl``.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

from ops.context import current_metrics

log = logging.getLogger("trace")

try:
    from opentelemetry import trace as _otel_trace
    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    _HAS_OTEL = False

_tracer = None


def setup_tracing(service_name: str = "support-agents") -> None:
    global _tracer
    if not _HAS_OTEL:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:  # api only
        _tracer = _otel_trace.get_tracer(service_name)
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:
            log.warning("OTLP exporter not installed; pip install opentelemetry-exporter-otlp-proto-http")
    if os.getenv("OTEL_CONSOLE") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    _otel_trace.set_tracer_provider(provider)
    _tracer = _otel_trace.get_tracer(service_name)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Context manager yielding a dict; add keys to it to attach attributes."""
    extra: dict[str, Any] = dict(attrs)
    t0 = time.perf_counter()
    otel_cm = _tracer.start_as_current_span(name) if _tracer else None
    otel_span = otel_cm.__enter__() if otel_cm else None
    status = "ok"
    try:
        yield extra
    except Exception:
        status = "error"
        raise
    finally:
        dur = round((time.perf_counter() - t0) * 1000, 2)
        trace_id = None
        if otel_span is not None:
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)):
                    otel_span.set_attribute(k, v)
            ctx = otel_span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
            otel_cm.__exit__(None, None, None)
        record = {"span": name, "duration_ms": dur, "status": status, "trace_id": trace_id, **extra}
        m = current_metrics()
        if m is not None:
            m.spans.append({"span": name, "duration_ms": dur, "status": status})
        log.info("span", extra={k: v for k, v in record.items() if k not in ("msg",)})
