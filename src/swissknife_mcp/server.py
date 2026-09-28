"""Builds the FastMCP server from discovered toolsets + configuration.

Nothing in here knows about any specific toolset: add a file to ``swissknife_mcp/tools``
and it is discovered, filtered by config, instantiated and registered automatically.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from swissknife_mcp import __version__
from swissknife_mcp.config import Settings
from swissknife_mcp.core.discovery import DEFAULT_TOOLS_PACKAGE, discover_toolsets
from swissknife_mcp.core.filtering import RequestToolFilterMiddleware, select_toolsets
from swissknife_mcp.core.toolset import (
    TOOLSET_TAG_PREFIX,
    BaseToolset,
    ToolsetDefinitionError,
    ToolsetRegistry,
    ToolSpec,
    registry,
)

logger = logging.getLogger(__name__)


@dataclass
class LoadedToolset:
    instance: BaseToolset
    tools: list[str] = field(default_factory=list)
    active: bool = True
    error: str | None = None


@dataclass
class ServerState:
    """Introspection data about what was loaded (also served on ``GET /toolsets``)."""

    toolsets: dict[str, LoadedToolset] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    tool_to_toolset: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "loaded": {
                name: {
                    "description": lt.instance.info.description,
                    "active": lt.active,
                    "error": lt.error,
                    "tools": lt.tools,
                }
                for name, lt in self.toolsets.items()
            },
            "skipped": self.skipped,
        }


class ToolCallLoggingMiddleware(Middleware):
    """Logs each tool call with its duration and outcome (arguments are not logged)."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning("tool %s failed in %.0fms: %s", name, elapsed, exc)
            raise
        logger.info("tool %s ok in %.0fms", name, (time.perf_counter() - start) * 1000)
        return result


def _annotations(spec: ToolSpec) -> ToolAnnotations | None:
    hints = {
        "title": spec.title,
        "read_only_hint": spec.read_only,
        "destructive_hint": spec.destructive,
        "idempotent_hint": spec.idempotent,
        "open_world_hint": spec.open_world,
    }
    hints = {k: v for k, v in hints.items() if v is not None}
    return ToolAnnotations(**hints) if hints else None


def _instructions(settings: Settings, state: ServerState) -> str:
    if settings.server.instructions:
        return settings.server.instructions
    lines = ["Multi-purpose MCP server. Available toolsets:"]
    for name, lt in state.toolsets.items():
        lines.append(f"- {name}: {lt.instance.info.description}".rstrip(": "))
    return "\n".join(lines)


def _build_tools(settings: Settings, reg: ToolsetRegistry, state: ServerState) -> list[Tool]:
    available = reg.names()
    include, exclude = settings.toolsets.include, settings.toolsets.exclude
    for name in [*include, *exclude]:
        if name != "all" and name not in reg:
            logger.warning(
                "Configured toolset '%s' does not exist (available: %s)", name, available
            )

    omit = set(settings.omit_tools)
    tools: list[Tool] = []

    for name in available:
        if name not in select_toolsets([name], include, exclude):
            state.skipped[name] = "disabled by configuration"
            continue
        info = reg.get(name)
        assert info is not None
        missing = info.missing_env()
        if missing:
            state.skipped[name] = f"missing environment variables: {', '.join(missing)}"
            logger.warning("Skipping toolset '%s': %s", name, state.skipped[name])
            continue

        instance = info.cls(config=settings.config_for(name))
        loaded = LoadedToolset(instance=instance)
        for method, spec in instance.get_tool_methods():
            tool_name = spec.name or method.__name__
            if tool_name in state.tool_to_toolset:
                raise ToolsetDefinitionError(
                    f"Duplicate tool name '{tool_name}' in toolsets "
                    f"'{state.tool_to_toolset[tool_name]}' and '{name}'"
                )
            state.tool_to_toolset[tool_name] = name
            if tool_name in omit:
                logger.info("Omitting tool '%s' (toolset '%s') per configuration", tool_name, name)
                continue
            tools.append(
                Tool.from_function(
                    method,
                    name=tool_name,
                    title=spec.title,
                    description=spec.description,
                    tags={f"{TOOLSET_TAG_PREFIX}{name}", *spec.tags},
                    annotations=_annotations(spec),
                    timeout=spec.timeout,
                    meta={"toolset": name, **spec.meta},
                )
            )
            loaded.tools.append(tool_name)
        state.toolsets[name] = loaded

    for tool_name in omit - set(state.tool_to_toolset):
        logger.warning("omit_tools entry '%s' does not match any loaded tool", tool_name)
    # Only keep mappings for tools actually exposed.
    state.tool_to_toolset = {t: ts for t, ts in state.tool_to_toolset.items() if t not in omit}
    return tools


def build_server(
    settings: Settings,
    *,
    tools_packages: list[str] | tuple[str, ...] = (DEFAULT_TOOLS_PACKAGE,),
    target_registry: ToolsetRegistry = registry,
    discover: bool = True,
) -> FastMCP:
    """Create a ready-to-run FastMCP server according to ``settings``."""
    if discover:
        discover_toolsets(tools_packages, target_registry=target_registry)

    state = ServerState()
    tools = _build_tools(settings, target_registry, state)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        started: list[LoadedToolset] = []
        for name, lt in state.toolsets.items():
            try:
                await lt.instance.setup()
                lt.active, lt.error = True, None
                started.append(lt)
            except Exception as exc:  # noqa: BLE001 - a broken toolset must not kill the server
                lt.active, lt.error = False, f"{type(exc).__name__}: {exc}"
                logger.exception("Toolset '%s' failed to set up; disabling its tools", name)
                server.disable(tags={f"{TOOLSET_TAG_PREFIX}{name}"})
        try:
            yield {"swissknife": state}
        finally:
            for lt in reversed(started):
                try:
                    await lt.instance.teardown()
                except Exception:  # noqa: BLE001
                    logger.exception("Toolset '%s' failed to tear down", lt.instance.name)

    mcp = FastMCP(
        name=settings.server.name,
        instructions=_instructions(settings, state),
        version=__version__,
        lifespan=lifespan,
        tools=tools,
        on_duplicate="error",
        # Tool exceptions are returned to the client as `isError` tool results by FastMCP
        # itself, so the LLM can read them; we only add timing/logging + request filtering.
        middleware=[
            ToolCallLoggingMiddleware(),
            RequestToolFilterMiddleware(state.tool_to_toolset),
        ],
    )

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @mcp.custom_route("/toolsets", methods=["GET"], include_in_schema=False)
    async def toolsets(_: Request) -> JSONResponse:
        return JSONResponse(state.summary())

    mcp.swissknife_state = state  # type: ignore[attr-defined]
    logger.info(
        "Loaded toolsets: %s | skipped: %s",
        {n: lt.tools for n, lt in state.toolsets.items()},
        state.skipped or "none",
    )
    return mcp
