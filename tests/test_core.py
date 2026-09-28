"""Tests for the toolset contract, config-driven selection, lifecycle and request filtering."""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from swissknife_mcp.config import Settings, load_settings
from swissknife_mcp.core.filtering import RequestFilter, select_toolsets
from swissknife_mcp.core.toolset import (
    BaseToolset,
    ToolsetDefinitionError,
    ToolsetRegistry,
    tool,
    toolset,
)
from swissknife_mcp.server import build_server

EVENTS: list[str] = []


def make_registry() -> ToolsetRegistry:
    reg = ToolsetRegistry()

    @toolset(name="alpha", description="Alpha tools", target_registry=reg)
    class Alpha(BaseToolset):
        async def setup(self) -> None:
            EVENTS.append("alpha.setup")
            self.greeting = self.config.get("greeting", "hello")

        async def teardown(self) -> None:
            EVENTS.append("alpha.teardown")

        @tool(read_only=True)
        async def greet(self, name: str) -> str:
            """Greet someone."""
            return f"{self.greeting} {name}"

        @tool(name="alpha_add")
        def add(self, a: int, b: int) -> int:
            """Add numbers."""
            return a + b

        def not_a_tool(self) -> None:  # pragma: no cover
            pass

    @toolset(name="beta", target_registry=reg)
    class Beta(BaseToolset):
        """Beta tools."""

        @tool
        async def beta_ping(self) -> str:
            """Ping."""
            return "pong"

    @toolset(name="broken", target_registry=reg)
    class Broken(BaseToolset):
        async def setup(self) -> None:
            raise RuntimeError("boom")

        @tool
        async def broken_tool(self) -> str:
            return "never"

    @toolset(name="needs_env", required_env=["SWISSKNIFE_TEST_MISSING_VAR"], target_registry=reg)
    class NeedsEnv(BaseToolset):
        @tool
        async def env_tool(self) -> str:
            return "x"

    return reg


def server(settings: Settings | None = None):
    return build_server(settings or Settings(), target_registry=make_registry(), discover=False)


async def tool_names(mcp) -> set[str]:
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


# ------------------------------------------------------------------ contract


def test_decorator_metadata():
    reg = make_registry()
    assert reg.names() == ["alpha", "beta", "broken", "needs_env"]
    beta = reg.get("beta")
    assert beta.description == "Beta tools."  # falls back to class docstring


def test_invalid_toolset_name():
    with pytest.raises(ToolsetDefinitionError):
        toolset(name="Bad Name")


def test_toolset_requires_base_class():
    with pytest.raises(ToolsetDefinitionError):

        @toolset(name="nope", target_registry=ToolsetRegistry())
        class NotAToolset:
            pass


def test_duplicate_toolset_name_rejected():
    reg = ToolsetRegistry()

    @toolset(name="dup", target_registry=reg)
    class One(BaseToolset):
        pass

    with pytest.raises(ToolsetDefinitionError):

        @toolset(name="dup", target_registry=reg)
        class Two(BaseToolset):
            pass


def test_duplicate_tool_name_across_toolsets_rejected():
    reg = ToolsetRegistry()

    @toolset(name="one", target_registry=reg)
    class One(BaseToolset):
        @tool
        async def same(self) -> str:
            return "1"

    @toolset(name="two", target_registry=reg)
    class Two(BaseToolset):
        @tool
        async def same(self) -> str:
            return "2"

    with pytest.raises(ToolsetDefinitionError, match="Duplicate tool name 'same'"):
        build_server(Settings(), target_registry=reg, discover=False)


# ---------------------------------------------------------- server selection


async def test_all_toolsets_loaded_by_default_and_env_gated():
    mcp = server()
    state = mcp.swissknife_state
    assert "needs_env" in state.skipped
    assert "missing environment variables" in state.skipped["needs_env"]
    names = await tool_names(mcp)
    # broken_tool is registered but disabled after its setup() fails in lifespan.
    assert names == {"greet", "alpha_add", "beta_ping"}
    assert state.toolsets["broken"].active is False
    assert "boom" in state.toolsets["broken"].error


async def test_include_exclude_and_omit_tools():
    settings = Settings.model_validate(
        {"toolsets": {"include": "alpha,beta", "exclude": ["beta"]}, "omit_tools": "alpha_add"}
    )
    assert await tool_names(server(settings)) == {"greet"}


async def test_tool_execution_uses_toolset_config_and_lifecycle():
    EVENTS.clear()
    settings = Settings.model_validate(
        {"toolsets": {"include": ["alpha"]}, "toolset_config": {"alpha": {"greeting": "hi"}}}
    )
    async with Client(server(settings)) as client:
        result = await client.call_tool("greet", {"name": "Bob"})
        assert result.data == "hi Bob"
        assert (await client.call_tool("alpha_add", {"a": 2, "b": 3})).data == 5
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["greet"].annotations.read_only_hint is True
        assert tools["greet"].description == "Greet someone."
    assert EVENTS == ["alpha.setup", "alpha.teardown"]


def test_select_toolsets():
    avail = ["a", "b", "c"]
    assert select_toolsets(avail, ["all"]) == avail
    assert select_toolsets(avail, []) == avail
    assert select_toolsets(avail, ["a", "c", "zzz"]) == ["a", "c"]
    assert select_toolsets(avail, ["all"], ["b"]) == ["a", "c"]


# ------------------------------------------------------- per-request filter


def test_request_filter_parsing():
    f = RequestFilter.from_mappings({"toolsets": "alpha, beta", "omit_tools": "greet"})
    assert f.toolsets == {"alpha", "beta"}
    assert f.omit_tools == {"greet"}
    assert not f.allows("greet", "alpha")
    assert f.allows("alpha_add", "alpha")
    assert not f.allows("x", "gamma")

    assert RequestFilter.from_mappings({"toolsets": "all"}).is_noop
    assert RequestFilter.from_mappings({}).is_noop
    hdr = RequestFilter.from_mappings({}, {"x-swissknife-toolsets": "beta"})
    assert hdr.toolsets == {"beta"}


async def test_request_filter_applied_via_middleware(monkeypatch):
    """Simulate an HTTP request carrying ?toolsets=alpha&omit_tools=greet."""
    import swissknife_mcp.core.filtering as filtering

    monkeypatch.setattr(
        filtering,
        "current_request_filter",
        lambda: RequestFilter.from_mappings({"toolsets": "alpha", "omit_tools": "greet"}),
    )
    async with Client(server()) as client:
        assert {t.name for t in await client.list_tools()} == {"alpha_add"}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("beta_ping", {})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("greet", {"name": "x"})


# ------------------------------------------------------------------- config


def test_load_settings_yaml_env_interpolation_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "server:\n  port: 9000\n"
        "toolsets:\n  include: [alpha]\n"
        "toolset_config:\n  alpha:\n    site: ${SK_TEST_SITE:-default.example}\n"
        "    token: ${SK_TEST_TOKEN}\n"
    )
    (tmp_path / ".env").write_text("SK_TEST_TOKEN=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SK_TEST_TOKEN", raising=False)
    monkeypatch.setenv("SWISSKNIFE_OMIT_TOOLS", "a,b")
    monkeypatch.setenv("SWISSKNIFE_PORT", "9100")

    settings = load_settings(cfg)
    assert settings.server.port == 9100
    assert settings.toolsets.include == ["alpha"]
    assert settings.omit_tools == ["a", "b"]
    assert settings.config_for("alpha") == {"site": "default.example", "token": "from-dotenv"}
    monkeypatch.delenv("SK_TEST_TOKEN", raising=False)


def test_discovery_finds_datadog():
    from swissknife_mcp.core.discovery import discover_toolsets
    from swissknife_mcp.core.toolset import registry

    result = discover_toolsets()
    assert "swissknife_mcp.tools.datadog" in result.imported
    assert "datadog" in registry
