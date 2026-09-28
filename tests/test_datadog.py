"""Datadog toolset tests with mocked HTTP (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from swissknife_mcp.config import Settings
from swissknife_mcp.server import build_server
from swissknife_mcp.tools.datadog import extract_widget_queries

API = "https://api.datadoghq.com"


@pytest.fixture
def dd_env(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "test-api-key")
    monkeypatch.setenv("DD_APP_KEY", "test-app-key")
    monkeypatch.delenv("DD_SITE", raising=False)


@pytest.fixture
def mcp(dd_env):
    settings = Settings.model_validate(
        {
            "toolsets": {"include": ["datadog"]},
            "toolset_config": {"datadog": {"site": "datadoghq.com"}},
        }
    )
    return build_server(settings)


def usage_item(i: int) -> dict:
    return {
        "id": f"id-{i}",
        "type": "dashboards-usages",
        "attributes": {"title": f"Dash {i}", "total_views": i, "widget_count": 3},
    }


DASHBOARD = {
    "id": "abc-def-ghi",
    "title": "Prod overview",
    "url": "/dashboard/abc-def-ghi/prod-overview",
    "layout_type": "ordered",
    "template_variables": [{"name": "env", "prefix": "env", "default": "prod"}],
    "widgets": [
        {
            "id": 1,
            "definition": {
                "type": "timeseries",
                "title": "CPU (legacy)",
                "requests": [{"q": "avg:system.cpu.user{$env} by {host}", "display_type": "line"}],
            },
        },
        {
            "id": 2,
            "definition": {
                "type": "group",
                "title": "APM",
                "widgets": [
                    {
                        "id": 21,
                        "definition": {
                            "type": "query_value",
                            "title": "Error rate",
                            "requests": [
                                {
                                    "queries": [
                                        {
                                            "name": "errors",
                                            "data_source": "metrics",
                                            "query": "sum:trace.http.request.errors{$env}.as_count()",
                                        },
                                        {
                                            "name": "logs",
                                            "data_source": "logs",
                                            "search": {"query": "status:error"},
                                            "indexes": ["main"],
                                            "compute": {"aggregation": "count"},
                                        },
                                    ],
                                    "formulas": [{"formula": "errors / logs"}],
                                }
                            ],
                        },
                    },
                    {"id": 22, "definition": {"type": "note", "content": "hello"}},
                ],
            },
        },
        {
            "id": 3,
            "definition": {
                "type": "log_stream",
                "title": "Errors",
                "query": "status:error service:web",
                "indexes": ["main"],
            },
        },
        {"id": 4, "definition": {"type": "slo", "title": "SLO", "slo_id": "slo123"}},
        {
            "id": 5,
            "definition": {
                "type": "toplist",
                "title": "Top logs (legacy)",
                "requests": [
                    {"log_query": {"index": "main", "search": {"query": "@http.status_code:500"}}}
                ],
            },
        },
        {
            "id": 6,
            "definition": {
                "type": "hostmap",
                "title": "Hosts",
                "requests": {"fill": {"q": "avg:system.load.1{*} by {host}"}},
            },
        },
        {
            "id": 7,
            "definition": {
                "type": "split_graph",
                "title": "Split",
                "source_widget_definition": {
                    "type": "timeseries",
                    "requests": [{"q": "avg:system.mem.used{*}"}],
                },
            },
        },
    ],
}


def test_extract_widget_queries():
    widgets = extract_widget_queries(DASHBOARD["widgets"])
    by_id = {w["widget_id"]: w for w in widgets}

    assert by_id[1]["queries"] == [
        {"data_source": "metrics", "query": "avg:system.cpu.user{$env} by {host}", "request": "0"}
    ]
    ev = by_id[21]
    assert ev["group_path"] == ["APM"]
    assert [q["query"] for q in ev["queries"]] == [
        "sum:trace.http.request.errors{$env}.as_count()",
        "status:error",
    ]
    assert ev["queries"][1]["indexes"] == ["main"]
    assert ev["formulas"][0]["formula"] == "errors / logs"
    assert 22 not in by_id  # note widget has no queries
    assert by_id[3]["queries"][0] == {"data_source": "logs", "query": "status:error service:web"}
    assert by_id[4]["references"] == {"slo_id": "slo123"}
    assert by_id[5]["queries"][0]["data_source"] == "log"
    assert by_id[5]["queries"][0]["query"] == "@http.status_code:500"
    assert by_id[6]["queries"][0]["request"] == "fill"
    assert by_id[7]["group_path"] == ["Split"]
    assert by_id[7]["queries"][0]["query"] == "avg:system.mem.used{*}"

    with_empty = extract_widget_queries(DASHBOARD["widgets"], include_empty=True)
    assert any(w["widget_id"] == 22 for w in with_empty)


async def test_tools_registered(mcp):
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {
        "list_dashboards_usage",
        "get_dashboard_usage",
        "list_dashboards",
        "get_dashboard",
        "get_dashboard_queries",
    }


@respx.mock
async def test_list_dashboards_usage_single_page(mcp):
    route = respx.get(f"{API}/api/v2/dashboards/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [usage_item(1), usage_item(2)],
                "meta": {"page": {"total": 10, "next_offset": 2}},
            },
        )
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "list_dashboards_usage", {"limit": 2, "edited_before": "2025-01-01T00:00:00Z"}
        )
    data = result.structured_content
    assert data["count"] == 2 and data["total"] == 10 and data["next_offset"] == 2
    assert data["dashboards"][0] == {
        "id": "id-1",
        "title": "Dash 1",
        "total_views": 1,
        "widget_count": 3,
    }
    req = route.calls.last.request
    assert req.headers["DD-API-KEY"] == "test-api-key"
    assert req.headers["DD-APPLICATION-KEY"] == "test-app-key"
    assert req.url.params["page[limit]"] == "2"
    assert req.url.params["filter[edited_before]"] == "2025-01-01T00:00:00Z"
    assert "filter[viewed_before]" not in req.url.params


@respx.mock
async def test_list_dashboards_usage_fetch_all(mcp):
    def responder(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["page[offset]"])
        pages = {0: ([usage_item(0), usage_item(1)], 2), 2: ([usage_item(2)], None)}
        data, nxt = pages[offset]
        return httpx.Response(
            200, json={"data": data, "meta": {"page": {"total": 3, "next_offset": nxt}}}
        )

    respx.get(f"{API}/api/v2/dashboards/usage").mock(side_effect=responder)
    async with Client(mcp) as client:
        result = await client.call_tool("list_dashboards_usage", {"limit": 2, "fetch_all": True})
    data = result.structured_content
    assert data["count"] == 3
    assert [d["id"] for d in data["dashboards"]] == ["id-0", "id-1", "id-2"]
    assert data["next_offset"] is None


@respx.mock
async def test_get_dashboard_usage(mcp):
    respx.get(f"{API}/api/v2/dashboards/abc/usage").mock(
        return_value=httpx.Response(200, json={"data": usage_item(7)})
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_dashboard_usage", {"dashboard_id": "abc"})
    assert result.structured_content["id"] == "id-7"
    assert result.structured_content["total_views"] == 7


@respx.mock
async def test_list_dashboards_with_title_filter(mcp):
    respx.get(f"{API}/api/v1/dashboard").mock(
        return_value=httpx.Response(
            200,
            json={
                "dashboards": [
                    {"id": "a", "title": "Prod Overview", "author_handle": "x", "url": "/a"},
                    {"id": "b", "title": "Staging", "url": "/b"},
                ]
            },
        )
    )
    async with Client(mcp) as client:
        result = await client.call_tool("list_dashboards", {"title_contains": "prod"})
    data = result.structured_content
    assert data["count"] == 1
    assert data["dashboards"][0]["id"] == "a"


@respx.mock
async def test_get_dashboard_and_queries(mcp):
    respx.get(f"{API}/api/v1/dashboard/abc-def-ghi").mock(
        return_value=httpx.Response(200, json=DASHBOARD)
    )
    async with Client(mcp) as client:
        raw = await client.call_tool("get_dashboard", {"dashboard_id": "abc-def-ghi"})
        assert raw.structured_content["title"] == "Prod overview"
        result = await client.call_tool("get_dashboard_queries", {"dashboard_id": "abc-def-ghi"})
    data = result.structured_content
    assert data["title"] == "Prod overview"
    assert data["template_variables"][0]["name"] == "env"
    assert data["widget_count"] == 7
    assert data["query_count"] == 7


@respx.mock
async def test_api_error_mapped_to_tool_error(mcp):
    respx.get(f"{API}/api/v1/dashboard/nope").mock(
        return_value=httpx.Response(404, json={"errors": ["Dashboard not found"]})
    )
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="404"):
            await client.call_tool("get_dashboard", {"dashboard_id": "nope"})


@respx.mock
async def test_retry_on_rate_limit(mcp, monkeypatch):
    import swissknife_mcp.tools.datadog as dd

    async def no_sleep(_):
        return None

    monkeypatch.setattr(dd.asyncio, "sleep", no_sleep)
    route = respx.get(f"{API}/api/v2/dashboards/abc/usage").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": "1"}),
            httpx.Response(200, json={"data": usage_item(1)}),
        ]
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_dashboard_usage", {"dashboard_id": "abc"})
    assert result.structured_content["id"] == "id-1"
    assert route.call_count == 2


def test_site_resolution(dd_env, monkeypatch):
    from swissknife_mcp.tools.datadog import DatadogToolset

    assert DatadogToolset({}).base_url == "https://api.datadoghq.com"
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    assert DatadogToolset({}).base_url == "https://api.datadoghq.eu"
    assert DatadogToolset({"site": "us5.datadoghq.com"}).base_url == "https://api.us5.datadoghq.com"
    assert DatadogToolset({"api_url": "http://proxy:1/"}).base_url == "http://proxy:1"
