"""Toolset contract: ``BaseToolset`` + the ``@toolset`` and ``@tool`` decorators.

A *toolset* is a named group of related tools (e.g. ``datadog``). Authoring one is a
single file in ``swissknife_mcp/tools/``::

    from swissknife_mcp.core import BaseToolset, tool, toolset

    @toolset(name="weather", description="Weather lookups", required_env=["WEATHER_API_KEY"])
    class WeatherToolset(BaseToolset):

        async def setup(self) -> None:          # optional, runs once on server start
            self.client = ...

        async def teardown(self) -> None:       # optional, runs once on server stop
            ...

        @tool(read_only=True)
        async def get_forecast(self, city: str, days: int = 3) -> dict:
            \"\"\"Docstring = tool description. Type hints = input schema.\"\"\"
            ...

``@toolset`` registers the class in a global registry at import time (like a Java
annotation processed by a classpath scanner); ``discovery.discover_toolsets()`` imports
every module in the ``tools`` package so dropping in a file is all it takes.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

logger = logging.getLogger(__name__)

TOOL_MARKER = "__swissknife_tool__"
TOOLSET_TAG_PREFIX = "toolset:"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]*$")

T = TypeVar("T", bound=type["BaseToolset"])
F = TypeVar("F", bound=Callable[..., Any])


class ToolsetDefinitionError(Exception):
    """Raised when a toolset or tool is declared incorrectly."""


@dataclass(frozen=True)
class ToolSpec:
    """Metadata captured by ``@tool`` on a toolset method."""

    name: str | None = None
    title: str | None = None
    description: str | None = None
    tags: frozenset[str] = frozenset()
    read_only: bool | None = None
    destructive: bool | None = None
    idempotent: bool | None = None
    open_world: bool | None = None
    timeout: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolsetInfo:
    """Metadata captured by ``@toolset`` on a ``BaseToolset`` subclass."""

    name: str
    description: str
    required_env: tuple[str, ...]
    cls: type[BaseToolset]
    module: str

    def missing_env(self) -> list[str]:
        return [var for var in self.required_env if not os.environ.get(var)]


class BaseToolset:
    """Base class every toolset must extend.

    Attributes available to subclasses:
        info:   ``ToolsetInfo`` (name, description, ...) set by ``@toolset``.
        config: per-toolset settings from ``config.yaml`` (``toolset_config.<name>``).
        logger: a logger named ``swissknife_mcp.toolsets.<name>``.
    """

    info: ClassVar[ToolsetInfo]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.logger = logging.getLogger(f"swissknife_mcp.toolsets.{self.name}")

    @property
    def name(self) -> str:
        return self.info.name

    async def setup(self) -> None:
        """Acquire resources (HTTP clients, DB pools, ...). Called once at server start."""

    async def teardown(self) -> None:
        """Release resources. Called once at server shutdown."""

    @staticmethod
    def env(var: str, default: str | None = None) -> str | None:
        """Convenience accessor for environment variables / ``.env`` secrets."""
        return os.environ.get(var, default)

    def get_tool_methods(self) -> list[tuple[Callable[..., Any], ToolSpec]]:
        """Return ``(bound_method, spec)`` for every ``@tool``-decorated method."""
        found: list[tuple[Callable[..., Any], ToolSpec]] = []
        seen: set[str] = set()
        # Walk the MRO so tools declared on mixins / parent toolsets are inherited.
        for klass in type(self).__mro__:
            for attr_name, attr in vars(klass).items():
                if attr_name in seen:
                    continue
                spec = getattr(attr, TOOL_MARKER, None)
                if spec is None:
                    continue
                seen.add(attr_name)
                found.append((getattr(self, attr_name), spec))
        return found


class ToolsetRegistry:
    """Registry of toolset classes populated by the ``@toolset`` decorator."""

    def __init__(self) -> None:
        self._toolsets: dict[str, ToolsetInfo] = {}

    def register(self, info: ToolsetInfo) -> None:
        existing = self._toolsets.get(info.name)
        if existing is not None and existing.cls is not info.cls:
            # Same qualified name => module was re-imported (e.g. reload); allow override.
            same = (
                existing.cls.__module__ == info.cls.__module__
                and existing.cls.__qualname__ == info.cls.__qualname__
            )
            if not same:
                raise ToolsetDefinitionError(
                    f"Toolset name '{info.name}' is already registered by "
                    f"{existing.cls.__module__}.{existing.cls.__qualname__}"
                )
        self._toolsets[info.name] = info

    def get(self, name: str) -> ToolsetInfo | None:
        return self._toolsets.get(name)

    def all(self) -> list[ToolsetInfo]:
        return sorted(self._toolsets.values(), key=lambda i: i.name)

    def names(self) -> list[str]:
        return [i.name for i in self.all()]

    def clear(self) -> None:
        self._toolsets.clear()

    def __contains__(self, name: object) -> bool:
        return name in self._toolsets

    def __len__(self) -> int:
        return len(self._toolsets)


registry = ToolsetRegistry()


def toolset(
    name: str,
    *,
    description: str = "",
    required_env: list[str] | tuple[str, ...] = (),
    target_registry: ToolsetRegistry | None = None,
) -> Callable[[T], T]:
    """Class decorator that declares and registers a toolset.

    Args:
        name: unique toolset id used in config and in ``?toolsets=`` (lowercase, ``[a-z0-9_-]``).
        description: human readable description (defaults to the class docstring).
        required_env: env vars that must be set; if any is missing the toolset is skipped
            at startup with a warning instead of exposing broken tools.
    """
    if not _NAME_RE.match(name):
        raise ToolsetDefinitionError(
            f"Invalid toolset name '{name}': use lowercase letters, digits, '_' or '-'"
        )

    def decorator(cls: T) -> T:
        if not (inspect.isclass(cls) and issubclass(cls, BaseToolset)):
            raise ToolsetDefinitionError(f"@toolset('{name}') must decorate a BaseToolset subclass")
        info = ToolsetInfo(
            name=name,
            description=description or inspect.getdoc(cls) or "",
            required_env=tuple(required_env),
            cls=cls,
            module=cls.__module__,
        )
        cls.info = info
        (registry if target_registry is None else target_registry).register(info)
        return cls

    return decorator


def tool(
    fn: F | None = None,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: set[str] | None = None,
    read_only: bool | None = None,
    destructive: bool | None = None,
    idempotent: bool | None = None,
    open_world: bool | None = None,
    timeout: float | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Method decorator that marks a toolset method as an MCP tool.

    Usable bare (``@tool``) or with options (``@tool(read_only=True)``). The method name
    becomes the tool name unless ``name`` is given; the docstring becomes the description.
    ``read_only`` / ``destructive`` / ``idempotent`` / ``open_world`` map to MCP tool
    annotation hints so clients can reason about side effects.
    """

    def decorator(func: F) -> F:
        if not callable(func):
            raise ToolsetDefinitionError("@tool must decorate a function")
        if func.__name__.startswith("_") and name is None:
            raise ToolsetDefinitionError(f"Private method '{func.__name__}' cannot be a tool")
        setattr(
            func,
            TOOL_MARKER,
            ToolSpec(
                name=name,
                title=title,
                description=description,
                tags=frozenset(tags or ()),
                read_only=read_only,
                destructive=destructive,
                idempotent=idempotent,
                open_world=open_world,
                timeout=timeout,
                meta=dict(meta or {}),
            ),
        )
        return func

    if fn is not None:
        return decorator(fn)
    return decorator
