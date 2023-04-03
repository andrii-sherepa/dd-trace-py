# Opentelemetry Tracer shim Unit Tests
from opentelemetry.trace import SpanKind as OtelSpanKind
from opentelemetry.trace.span import TraceFlags
from opentelemetry.trace.status import Status as OtelStatus
from opentelemetry.trace.status import StatusCode as OtelStatusCode
import pytest

from ddtrace.constants import MANUAL_DROP_KEY


@pytest.mark.snapshot
def test_otel_span_attributes(oteltracer):
    with oteltracer.start_span("otel-string-tags") as span1:
        span1.set_attribute("service.name", "moons-service-str")
        span1.set_attribute(u"unicode_tag", u"ustr")
        # b"bytes_tag" is ignored by dd_span.set_tag()
        span1.set_attribute(b"bytes_tag", b"bstr")
        span1.set_attribute(r"real_string_tag", r"rstr")
        span1.set_attributes({"tag1": "one", "tag2": "two", "tag3": "3"})

    with oteltracer.start_span("otel-numerical-tags") as span2:
        span2.set_attribute("service.name", "moons-service-num")
        span2.set_attribute("int_tag", 1)
        span2.set_attribute("float_tag", 2.111)
        span2.set_attributes({"tag1": 1, "tag2": 2, "tag3": 3.1415})
        span2.end()

    # Attributes should not be set on a closed span
    for span in [span1, span2]:
        span.set_attribute("should_not_be_set", "attributes can not be added after a span is ended")


@pytest.mark.snapshot
def test_otel_span_kind(oteltracer):
    with oteltracer.start_span("otel-client", kind=OtelSpanKind.CLIENT):
        pass
    with oteltracer.start_span("otel-server", kind=OtelSpanKind.SERVER):
        pass
    with oteltracer.start_span("otel-producer", kind=OtelSpanKind.PRODUCER):
        pass
    with oteltracer.start_span("otel-consumer", kind=OtelSpanKind.CONSUMER):
        pass
    with oteltracer.start_span("otel-internal", kind=OtelSpanKind.INTERNAL):
        pass


def test_otel_span_status_with_status_obj(oteltracer):
    with oteltracer.start_span("otel-unset") as unsetspan:
        unsetspan.set_status(OtelStatus(OtelStatusCode.UNSET, None))
        assert unsetspan._ddspan.error == 0

    with oteltracer.start_span("otel-ok") as okspan:
        okspan.set_status(OtelStatus(OtelStatusCode.OK, "ok was set"))
        assert okspan._ddspan.error == 0

    with oteltracer.start_span("otel-error") as errspan:
        errspan.set_status(OtelStatus(OtelStatusCode.ERROR, "error message for otel span"))
        assert errspan._ddspan.error == 1

    with oteltracer.start_span("set-status-on-otel-span") as span1:
        pass

    # can not update status on closed span
    assert span1._ddspan.error == 0
    span1.set_status(OtelStatus(OtelStatusCode.ERROR, "error message for otel span"))
    assert span1._ddspan.error == 0


def test_otel_span_status_with_status_code(oteltracer):
    with oteltracer.start_span("otel-unset") as unsetspan:
        unsetspan.set_status(OtelStatusCode.UNSET, "is unset")
        assert unsetspan._ddspan.error == 0

    with oteltracer.start_span("otel-ok") as okspan:
        okspan.set_status(OtelStatusCode.OK, None)
        assert okspan._ddspan.error == 0

    with oteltracer.start_span("otel-error") as errspan:
        errspan.set_status(OtelStatusCode.ERROR, "error message for otel span")
        assert errspan._ddspan.error == 1

    with oteltracer.start_span("set-status-code-on-otel-span") as span2:
        pass
    # can not update status on closed span
    assert span2._ddspan.error == 0
    span2.set_status(OtelStatusCode.ERROR, "error message for otel span")
    assert span2._ddspan.error == 0


def test_otel_add_event(oteltracer):
    with oteltracer.start_span("otel-client") as client:
        client.add_event("no op event", dict(), 1671826913)
        client.add_event("no op event", {"hi": "monkey"}, None)
    assert client._ddspan.error == 0


def test_otel_update_span_name(oteltracer):
    with oteltracer.start_span("otel-server") as server:
        assert server._ddspan.name == "otel-server"
        server.update_name("renamed-otel-server")
    assert server._ddspan.name == "renamed-otel-server"


def test_otel_span_is_recording(oteltracer):
    with oteltracer.start_span("otel1") as span:
        assert span.is_recording() is True
    assert span.is_recording() is False


def test_otel_span_exception_handling(oteltracer):
    with pytest.raises(Exception):
        with oteltracer.start_span("otel1") as span:
            raise Exception("Sorry Friend, I failed you")

    assert span._ddspan.error == 1
    assert span._ddspan._meta["error.message"] == "Sorry Friend, I failed you"
    assert span._ddspan._meta["error.type"] == "builtins.Exception"
    assert span._ddspan._meta["error.stack"] is not None


def test_otel_get_span_context(oteltracer):
    otelspan = oteltracer.start_span("otel-server")

    span_context = otelspan.get_span_context()
    # Ensure span id and trace id stored in ddtrace.context.Context._traceparent is consistent
    # with the ids returned by ddtrace._opentelemetry.span.Span.get_span_context()
    _, trace_id_expected, span_id_expected, _ = otelspan._ddspan._context._traceparent.split("-")
    assert span_context.trace_id == trace_id_expected
    assert span_context.span_id == span_id_expected
    # A ddtrace._opentelemetry.Span can never be remote.
    # opentelemetry.trace.NonRecordingSpan is used to represent a "remote span".
    assert span_context.is_remote is False
    # By default ddtrace set sampled=True for all spans
    assert span_context.trace_flags == TraceFlags.SAMPLED
    # Default tracestate values set on all Datadog Spans
    assert span_context.trace_state.to_header() == "dd=s:1;t.dm:-0"


def test_otel_get_span_context_with_multiple_tracesates(oteltracer):
    otelspan = oteltracer.start_span("otel-server")
    otelspan._ddspan._context._meta["_dd.p.congo"] = "t61rcWkgMzE"
    otelspan._ddspan._context._meta["_dd.p.some_val"] = "tehehe"

    span_context = otelspan.get_span_context()
    assert span_context.trace_state.to_header() == "dd=s:1;t.dm:-0;t.congo:t61rcWkgMzE;t.some_val:tehehe"


def test_otel_get_span_context_with_default_trace_state(oteltracer):
    otelspan = oteltracer.start_span("otel-server")
    otelspan.set_attribute(MANUAL_DROP_KEY, "")

    span_context = otelspan.get_span_context()
    assert span_context.trace_flags == TraceFlags.DEFAULT
