"""Toolset / tool selection logic.

Two layers use the same rules:

* **Server-side** (``config.yaml`` / env / CLI): decides which toolsets and tools are
  registered at all. Anything filtered here does not exist for any client.
* **Per-request** (MCP URL query string, e.g. ``/mcp?toolsets=datadog&omit_tools=x,y``):
  ``RequestToolFilterMiddleware`` narrows what a particular client sees and may call.
  It can only narrow - never expose something the server did not register.

HTTP headers ``X-Swissknife-Toolsets`` / ``X-Swissknife-Omit-Tools`` are accepted as an
alternative for clients that cannot put query parameters in the URL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from swissknife_mcp.config import ALL, split_csv

logger = logging.getLogger(__name__)

TOOLSETS_PARAM = "toolsets"
OMIT_TOOLS_PARAM = "omit_tools"
TOOLSETS_HEADER = "x-swissknife-toolsets"
OMIT_TOOLS_HEADER = "x-swissknife-omit-tools"


def select_toolsets(
    available: Iterable[str],
    include: Iterable[str] | None,
    exclude: Iterable[str] | None = None,
) -> list[str]:
    """Return the toolset names from ``available`` matching include/exclude rules.

    ``include`` empty/None or containing ``"all"`` means every available toolset.
    Exclusions always win over inclusions.
    """
    available = list(available)
    include_set = set(include or [])
    exclude_set = set(exclude or [])
    if not include_set or ALL in include_set:
        selected = available
    else:
        selected = [name for name in available if name in include_set]
    return [name for name in selected if name not in exclude_set]


@dataclass(frozen=True)
class RequestFilter:
    """Filter parsed from a single client request. ``toolsets=None`` means no restriction."""

    toolsets: frozenset[str] | None = None
    omit_tools: frozenset[str] = frozenset()

    @property
    def is_noop(self) -> bool:
        return self.toolsets is None and not self.omit_tools

    def allows(self, tool_name: str, toolset_name: str | None) -> bool:
        if tool_name in self.omit_tools:
            return False
        if self.toolsets is None:
            return True
        return toolset_name is not None and toolset_name in self.toolsets

    @classmethod
    def from_mappings(
        cls, query: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> RequestFilter:
        headers = headers or {}
        toolsets_raw = query.get(TOOLSETS_PARAM) or headers.get(TOOLSETS_HEADER)
        omit_raw = query.get(OMIT_TOOLS_PARAM) or headers.get(OMIT_TOOLS_HEADER)
        toolsets = split_csv(toolsets_raw)
        return cls(
            toolsets=None if not toolsets or ALL in toolsets else frozenset(toolsets),
            omit_tools=frozenset(split_csv(omit_raw)),
        )


def current_request_filter() -> RequestFilter:
    """Build a ``RequestFilter`` from the active HTTP request (no-op for stdio/in-memory)."""
    try:
        request = get_http_request()
    except RuntimeError:
        return RequestFilter()
    return RequestFilter.from_mappings(request.query_params, request.headers)


class RequestToolFilterMiddleware(Middleware):
    """Applies ``?toolsets=`` / ``?omit_tools=`` to ``tools/list`` and ``tools/call``."""

    def __init__(self, tool_to_toolset: Mapping[str, str]) -> None:
        self._tool_to_toolset = dict(tool_to_toolset)

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        request_filter = current_request_filter()
        if request_filter.is_noop:
            return tools
        return [
            t for t in tools if request_filter.allows(t.name, self._tool_to_toolset.get(t.name))
        ]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        request_filter = current_request_filter()
        name = context.message.name
        if not request_filter.is_noop and not request_filter.allows(
            name, self._tool_to_toolset.get(name)
        ):
            logger.info("Blocked call to tool '%s' filtered out by request parameters", name)
            raise ToolError(f"Unknown tool: {name}")
        return await call_next(context)
