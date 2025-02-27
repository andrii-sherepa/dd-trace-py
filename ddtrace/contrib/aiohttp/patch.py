import os
import typing

from yarl import URL

from ddtrace import config
from ddtrace.constants import SPAN_KIND
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils.formats import asbool
from ddtrace.vendor import wrapt

from ...ext import SpanKind
from ...ext import SpanTypes
from ...internal.compat import parse
from ...internal.schema import schematize_url_operation
from ...pin import Pin
from ...propagation.http import HTTPPropagator
from ..trace_utils import ext_service
from ..trace_utils import set_http_meta
from ..trace_utils import unwrap
from ..trace_utils import with_traced_module as with_traced_module_sync
from ..trace_utils import wrap
from ..trace_utils_async import with_traced_module


log = get_logger(__name__)


# Server config
config._add(
    "aiohttp",
    dict(distributed_tracing=True),
)

config._add(
    "aiohttp_client",
    dict(
        distributed_tracing=asbool(os.getenv("DD_AIOHTTP_CLIENT_DISTRIBUTED_TRACING", True)),
        default_http_tag_query_string=os.getenv("DD_HTTP_CLIENT_TAG_QUERY_STRING", "true"),
    ),
)


class _WrappedConnectorClass(wrapt.ObjectProxy):
    def __init__(self, obj, pin):
        super().__init__(obj)
        pin.onto(self)

    async def connect(self, req, *args, **kwargs):
        pin = Pin.get_from(self)
        with pin.tracer.trace("%s.connect" % self.__class__.__name__) as span:
            # set component tag equal to name of integration
            span.set_tag(COMPONENT, config.aiohttp.integration_name)
            result = await self.__wrapped__.connect(req, *args, **kwargs)
            return result

    async def _create_connection(self, req, *args, **kwargs):
        pin = Pin.get_from(self)
        with pin.tracer.trace("%s._create_connection" % self.__class__.__name__) as span:
            # set component tag equal to name of integration
            span.set_tag(COMPONENT, config.aiohttp.integration_name)
            result = await self.__wrapped__._create_connection(req, *args, **kwargs)
            return result


def extract_info_from_url(url):
    # type: (str) -> typing.Tuple[str, str]
    parse_result = parse.urlparse(url)
    query = parse_result.query

    # Relative URLs don't have a netloc, so we force them
    if not parse_result.netloc:
        parse_result = parse.urlparse("//{url}".format(url=url))

    netloc = parse_result.netloc.split("@", 1)[-1]  # Discard auth info
    netloc = netloc.split(":", 1)[0]  # Discard port information
    return netloc, query


@with_traced_module
async def _traced_clientsession_request(aiohttp, pin, func, instance, args, kwargs):
    method = get_argument_value(args, kwargs, 0, "method")  # type: str
    url = URL(get_argument_value(args, kwargs, 1, "url"))  # type: URL
    params = kwargs.get("params")
    headers = kwargs.get("headers") or {}

    with pin.tracer.trace(
        schematize_url_operation("aiohttp.request", protocol="http", direction="outbound"),
        span_type=SpanTypes.HTTP,
        service=ext_service(pin, config.aiohttp_client),
    ) as span:
        if pin._config["distributed_tracing"]:
            HTTPPropagator.inject(span.context, headers)
            kwargs["headers"] = headers

        span.set_tag_str(COMPONENT, config.aiohttp_client.integration_name)

        # set span.kind tag equal to type of request
        span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

        # Params can be included separate of the URL so the URL has to be constructed
        # with the passed params.
        url_str = str(url.update_query(params) if params else url)
        host, query = extract_info_from_url(url_str)
        set_http_meta(
            span,
            config.aiohttp_client,
            method=method,
            url=str(url),
            target_host=host,
            query=query,
            request_headers=headers,
        )
        resp = await func(*args, **kwargs)  # type: aiohttp.ClientResponse
        set_http_meta(
            span, config.aiohttp_client, response_headers=resp.headers, status_code=resp.status, status_msg=resp.reason
        )
        return resp


@with_traced_module_sync
def _traced_clientsession_init(aiohttp, pin, func, instance, args, kwargs):
    func(*args, **kwargs)
    instance._connector = _WrappedConnectorClass(instance._connector, pin)


def _patch_client(aiohttp):
    Pin().onto(aiohttp)
    pin = Pin(_config=config.aiohttp_client.copy())
    pin.onto(aiohttp.ClientSession)

    wrap("aiohttp", "ClientSession.__init__", _traced_clientsession_init(aiohttp))
    wrap("aiohttp", "ClientSession._request", _traced_clientsession_request(aiohttp))


def patch():
    import aiohttp

    if getattr(aiohttp, "_datadog_patch", False):
        return

    _patch_client(aiohttp)

    setattr(aiohttp, "_datadog_patch", True)


def _unpatch_client(aiohttp):
    unwrap(aiohttp.ClientSession, "__init__")
    unwrap(aiohttp.ClientSession, "_request")


def unpatch():
    import aiohttp

    if not getattr(aiohttp, "_datadog_patch", False):
        return

    _unpatch_client(aiohttp)

    setattr(aiohttp, "_datadog_patch", False)
