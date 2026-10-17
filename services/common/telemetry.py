"""OpenTelemetry setup shared by the agent, gateway and replay server.

Spans go over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT (Jaeger locally, a hosted backend
in production). OTEL_SDK_DISABLED=true disables it. Trace context crosses process
boundaries inside MCP request `_meta` (`traceparent`), which the MCP SDK handles.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from services.common.config import Settings


def setup_tracing(service_name: str, settings: Settings) -> TracerProvider | None:
    """Install a global tracer provider for this process. Returns None when disabled.

    Call `provider.shutdown()` before exit so batched spans are flushed. If the backend is
    down, the exporter logs a warning and drops spans; it never breaks a tool call.
    """
    endpoint = settings.otel_exporter_otlp_endpoint.strip().rstrip("/")
    if settings.otel_sdk_disabled or not endpoint:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": settings.tradeguard_env}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    return provider


def current_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the current span as hex, or (None, None) when not tracing."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")
