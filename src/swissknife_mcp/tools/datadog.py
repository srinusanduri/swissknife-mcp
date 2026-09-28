"""Datadog toolset: dashboards listing, definitions, usage stats and widget queries.

Secrets (``.env``):
    DD_API_KEY, DD_APP_KEY   required
    DD_SITE                  optional, e.g. ``datadoghq.eu`` / ``us5.datadoghq.com``

Optional ``config.yaml``::

    toolset_config:
      datadog:
        site: datadoghq.com        # overrides DD_SITE
        api_url: https://api.datadoghq.com   # full override (proxies, tests)
        timeout: 30
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from swissknife_mcp.core import BaseToolset, raise_for_status, tool, toolset

SERVICE = "Datadog"
DEFAULT_SITE = "datadoghq.com"
USAGE_PAGE_MAX = 500
_RETRY_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES = 3

# Widget-level string `query` fields -> data source they target.
_WIDGET_QUERY_SOURCES = {
    "event_stream": "events",
    "event_timeline": "events",
    "log_stream": "logs",
    "monitor_summary": "monitors",
}
# Widget-level fields that reference other Datadog objects instead of holding a query.
_WIDGET_REFERENCE_FIELDS = (
    "slo_id",
    "alert_id",
    "check",
    "group",
    "tags",
    "service",
    "env",
    "span_name",
    "powerpack_id",
    "app_id",
    "workflow_id",
    "url",
    "indexes",
    "scope",
)
# Legacy per-product request keys, e.g. {"log_query": {"index": "main", "search": {...}}}.
_LEGACY_QUERY_KEYS = (
    "log_query",
    "apm_query",
    "rum_query",
    "event_query",
    "security_query",
    "network_query",
    "audit_query",
    "profile_metrics_query",
)


def _flatten_jsonapi(item: dict[str, Any]) -> dict[str, Any]:
    """``{"id": x, "type": t, "attributes": {...}}`` -> ``{"id": x, ...attributes}``."""
    return {"id": item.get("id"), **(item.get("attributes") or {})}


def _query_string(obj: dict[str, Any]) -> str | None:
    """Best-effort extraction of the human-readable query string from a query object."""
    for key in ("query", "query_string", "q"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    search = obj.get("search")
    if isinstance(search, dict) and isinstance(search.get("query"), str):
        return search["query"]
    return None


def _normalize_query(obj: dict[str, Any], default_source: str | None) -> dict[str, Any]:
    """Normalize a query object to ``{name, data_source, query, ...other fields}``."""
    rest = {k: v for k, v in obj.items() if k not in ("name", "data_source")}
    out: dict[str, Any] = {}
    if "name" in obj:
        out["name"] = obj["name"]
    out["data_source"] = obj.get("data_source") or default_source
    out["query"] = _query_string(obj)
    # Drop the fields we already surfaced as `query` to keep payloads compact.
    for key in ("query", "query_string", "q"):
        if isinstance(rest.get(key), str):
            rest.pop(key)
    if isinstance(rest.get("search"), dict) and set(rest["search"]) == {"query"}:
        rest.pop("search")
    out.update(rest)
    return out


def _extract_request_queries(
    request: dict[str, Any], widget_type: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(queries, formulas)`` from one widget request (legacy or formula style)."""
    queries: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []

    # Legacy metric query string.
    if isinstance(request.get("q"), str):
        queries.append({"data_source": "metrics", "query": request["q"]})

    # Formula & functions style.
    for item in request.get("queries") or []:
        if isinstance(item, dict):
            queries.append(_normalize_query(item, default_source=None))
    for formula in request.get("formulas") or []:
        if isinstance(formula, dict):
            formulas.append(formula)

    # Legacy per-product queries.
    for key in _LEGACY_QUERY_KEYS:
        obj = request.get(key)
        if isinstance(obj, dict):
            queries.append(_normalize_query(obj, default_source=key.removesuffix("_query")))
    if isinstance(request.get("process_query"), dict):
        pq = request["process_query"]
        queries.append({"data_source": "process", "query": pq.get("metric"), **pq})
    if isinstance(request.get("apm_stats_query"), dict):
        queries.append({"data_source": "apm_stats", "query": None, **request["apm_stats_query"]})

    # Single `query` object (list_stream, funnel, slo_list, cohort, point_plot, ...).
    single = request.get("query")
    if isinstance(single, dict):
        queries.append(_normalize_query(single, default_source=widget_type))
    elif isinstance(single, str):
        queries.append({"data_source": widget_type, "query": single})

    return queries, formulas


def _iter_requests(requests: Any) -> list[tuple[str, dict[str, Any]]]:
    """Requests are a list for most widgets, but a dict for hostmap (fill/size) and
    scatterplot (x/y/table). Return ``(label, request)`` pairs for both shapes."""
    if isinstance(requests, list):
        return [(str(i), r) for i, r in enumerate(requests) if isinstance(r, dict)]
    if isinstance(requests, dict):
        pairs: list[tuple[str, dict[str, Any]]] = []
        for label, value in requests.items():
            if isinstance(value, dict):
                pairs.append((label, value))
            elif isinstance(value, list):
                pairs.extend(
                    (f"{label}.{i}", v) for i, v in enumerate(value) if isinstance(v, dict)
                )
        return pairs
    return []


def extract_widget_queries(
    widgets: list[dict[str, Any]],
    path: tuple[str, ...] = (),
    include_empty: bool = False,
) -> list[dict[str, Any]]:
    """Walk dashboard widgets (recursing into groups/powerpacks/split graphs) and
    return one record per widget with all its queries and formulas."""
    results: list[dict[str, Any]] = []
    for widget in widgets or []:
        definition = widget.get("definition") or {}
        widget_type = definition.get("type", "unknown")
        title = definition.get("title") or ""

        # Container widgets: recurse, prefixing the group title to the path.
        nested = definition.get("widgets")
        if isinstance(nested, list):
            label = title or f"{widget_type}:{widget.get('id')}"
            results.extend(extract_widget_queries(nested, (*path, label), include_empty))
            continue
        if widget_type == "split_graph" and isinstance(
            definition.get("source_widget_definition"), dict
        ):
            source = {"id": widget.get("id"), "definition": definition["source_widget_definition"]}
            label = title or f"split_graph:{widget.get('id')}"
            results.extend(extract_widget_queries([source], (*path, label), include_empty))
            continue

        queries: list[dict[str, Any]] = []
        formulas: list[dict[str, Any]] = []
        for label, request in _iter_requests(definition.get("requests")):
            req_queries, req_formulas = _extract_request_queries(request, widget_type)
            for q in req_queries:
                q.setdefault("request", label)
            for f in req_formulas:
                f.setdefault("request", label)
            queries.extend(req_queries)
            formulas.extend(req_formulas)

        widget_query = definition.get("query")
        if isinstance(widget_query, str):
            queries.append(
                {
                    "data_source": _WIDGET_QUERY_SOURCES.get(widget_type, widget_type),
                    "query": widget_query,
                }
            )

        references = {
            k: definition[k]
            for k in _WIDGET_REFERENCE_FIELDS
            if definition.get(k) not in (None, "", [])
        }

        if not (queries or formulas or references) and not include_empty:
            continue
        record: dict[str, Any] = {
            "widget_id": widget.get("id"),
            "widget_type": widget_type,
            "title": title,
            "group_path": list(path),
            "queries": queries,
        }
        if formulas:
            record["formulas"] = formulas
        if references:
            record["references"] = references
        results.append(record)
    return results


@toolset(
    name="datadog",
    description="Datadog dashboards: list, definitions, usage statistics and widget queries.",
    required_env=["DD_API_KEY", "DD_APP_KEY"],
)
class DatadogToolset(BaseToolset):
    client: httpx.AsyncClient

    @property
    def base_url(self) -> str:
        if self.config.get("api_url"):
            return str(self.config["api_url"]).rstrip("/")
        site = self.config.get("site") or self.env("DD_SITE") or DEFAULT_SITE
        site = str(site).removeprefix("https://").removeprefix("api.").rstrip("/")
        return f"https://api.{site}"

    async def setup(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/json",
                "DD-API-KEY": self.env("DD_API_KEY") or "",
                "DD-APPLICATION-KEY": self.env("DD_APP_KEY") or "",
                "User-Agent": "swissknife-mcp",
            },
            timeout=float(self.config.get("timeout", 30)),
        )
        self.logger.info("Datadog toolset ready (api: %s)", self.base_url)

    async def teardown(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with retries on rate limiting / transient 5xx, honoring Datadog's
        ``X-RateLimit-Reset`` header when present."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                response = await self.client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise ToolError(f"{SERVICE} API request timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ToolError(f"{SERVICE} API request failed: {exc}") from exc
            if response.status_code not in _RETRY_STATUSES or attempt >= _MAX_RETRIES:
                break
            reset = response.headers.get("X-RateLimit-Reset", "")
            delay = min(float(reset), 30.0) if reset.isdigit() else float(2**attempt)
            self.logger.warning(
                "Datadog returned %s on %s, retrying in %.0fs", response.status_code, path, delay
            )
            await asyncio.sleep(delay)
            attempt += 1
        raise_for_status(response, SERVICE)
        return response.json() if response.content else {}

    # ------------------------------------------------------------------ usage

    @tool(read_only=True, idempotent=True, open_world=True)
    async def list_dashboards_usage(
        self,
        limit: Annotated[
            int, Field(ge=1, le=USAGE_PAGE_MAX, description="Page size (max 500).")
        ] = 250,
        offset: Annotated[int, Field(ge=0, description="Zero-based offset.")] = 0,
        edited_before: Annotated[
            str | None,
            Field(
                description="Only dashboards last edited before this ISO 8601 timestamp "
                "with timezone, e.g. 2025-04-26T00:00:00Z."
            ),
        ] = None,
        viewed_before: Annotated[
            str | None,
            Field(
                description="Only dashboards last viewed before this ISO 8601 timestamp "
                "with timezone (includes never-viewed dashboards)."
            ),
        ] = None,
        fetch_all: Annotated[
            bool, Field(description="Follow pagination and return every page (up to max_results).")
        ] = False,
        max_results: Annotated[
            int, Field(ge=1, description="Safety cap on dashboards returned when fetch_all=true.")
        ] = 5000,
    ) -> dict[str, Any]:
        """Get usage statistics for all dashboards in the org (GET /api/v2/dashboards/usage).

        Per dashboard: title, author, created/edited/viewed timestamps, last viewer, total
        views (by type), widget counts (by type), teams and quality score. Useful for
        finding stale, unused or most popular dashboards. View counts refresh daily, only
        cover views since January 2025, and require RUM (otherwise null/0).
        """
        filters = {"filter[edited_before]": edited_before, "filter[viewed_before]": viewed_before}
        dashboards: list[dict[str, Any]] = []
        current_offset = offset
        page_meta: dict[str, Any] = {}
        while True:
            page_limit = min(limit, max_results - len(dashboards)) if fetch_all else limit
            payload = await self._get(
                "/api/v2/dashboards/usage",
                {"page[limit]": page_limit, "page[offset]": current_offset, **filters},
            )
            data = payload.get("data") or []
            dashboards.extend(_flatten_jsonapi(item) for item in data)
            page_meta = (payload.get("meta") or {}).get("page") or {}
            next_offset = page_meta.get("next_offset")
            if not fetch_all or not data or next_offset is None or len(dashboards) >= max_results:
                break
            current_offset = next_offset
        return {
            "count": len(dashboards),
            "total": page_meta.get("total"),
            "next_offset": None
            if fetch_all and len(dashboards) < max_results
            else page_meta.get("next_offset"),
            "dashboards": dashboards,
        }

    @tool(read_only=True, idempotent=True, open_world=True)
    async def get_dashboard_usage(
        self,
        dashboard_id: Annotated[str, Field(description="Dashboard id, e.g. 'abc-def-ghi'.")],
    ) -> dict[str, Any]:
        """Get usage statistics for one dashboard (GET /api/v2/dashboards/{id}/usage):
        views, last viewer, edit/view times, widget counts and quality score."""
        payload = await self._get(f"/api/v2/dashboards/{dashboard_id}/usage")
        return _flatten_jsonapi(payload.get("data") or {})

    # ------------------------------------------------------------- dashboards

    @tool(read_only=True, idempotent=True, open_world=True)
    async def list_dashboards(
        self,
        title_contains: Annotated[
            str | None, Field(description="Case-insensitive substring filter on title.")
        ] = None,
        shared_only: Annotated[bool, Field(description="Only shared dashboards.")] = False,
        include_deleted: Annotated[bool, Field(description="Only deleted dashboards.")] = False,
        count: Annotated[int, Field(ge=1, le=1000, description="Max dashboards to return.")] = 100,
        start: Annotated[int, Field(ge=0, description="Offset into the results.")] = 0,
    ) -> dict[str, Any]:
        """List dashboards (GET /api/v1/dashboard) with id, title, url, author and
        timestamps. Use it to find dashboard ids for the other dashboard tools."""
        params: dict[str, Any] = {
            "filter[shared]": "true" if shared_only else None,
            "filter[deleted]": "true" if include_deleted else None,
        }
        if not title_contains:
            params.update({"count": count, "start": start})
        payload = await self._get("/api/v1/dashboard", params)
        dashboards = payload.get("dashboards") or []
        if title_contains:
            needle = title_contains.lower()
            dashboards = [d for d in dashboards if needle in (d.get("title") or "").lower()]
            total = len(dashboards)
            dashboards = dashboards[start : start + count]
        else:
            total = None
        fields = (
            "id",
            "title",
            "description",
            "url",
            "layout_type",
            "author_handle",
            "created_at",
            "modified_at",
            "is_read_only",
            "deleted_at",
        )
        return {
            "count": len(dashboards),
            "total_matching": total,
            "dashboards": [
                {k: d.get(k) for k in fields if d.get(k) is not None} for d in dashboards
            ],
        }

    @tool(read_only=True, idempotent=True, open_world=True)
    async def get_dashboard(
        self,
        dashboard_id: Annotated[str, Field(description="Dashboard id, e.g. 'abc-def-ghi'.")],
    ) -> dict[str, Any]:
        """Get the full raw dashboard definition JSON (GET /api/v1/dashboard/{id}),
        including all widgets, layout and template variables. Can be large - prefer
        get_dashboard_queries when you only need the queries."""
        return await self._get(f"/api/v1/dashboard/{dashboard_id}")

    @tool(read_only=True, idempotent=True, open_world=True)
    async def get_dashboard_queries(
        self,
        dashboard_id: Annotated[str, Field(description="Dashboard id, e.g. 'abc-def-ghi'.")],
        include_empty_widgets: Annotated[
            bool, Field(description="Also list widgets without queries (notes, images, ...).")
        ] = False,
    ) -> dict[str, Any]:
        """Get every query used by each widget/panel of a dashboard.

        Recurses into group, powerpack and split-graph widgets. For each widget returns
        id, type, title, group path, and normalized queries (data_source, name, query
        string plus extra fields such as aggregator, compute, group_by, indexes), the
        formulas combining them, and references (slo_id, alert_id, ...). Dashboard
        template variables are returned too, since queries often use $variables.
        """
        dashboard = await self._get(f"/api/v1/dashboard/{dashboard_id}")
        widgets = extract_widget_queries(
            dashboard.get("widgets") or [], include_empty=include_empty_widgets
        )
        return {
            "dashboard_id": dashboard.get("id", dashboard_id),
            "title": dashboard.get("title"),
            "url": dashboard.get("url"),
            "layout_type": dashboard.get("layout_type"),
            "template_variables": dashboard.get("template_variables") or [],
            "widget_count": len(widgets),
            "query_count": sum(len(w["queries"]) for w in widgets),
            "widgets": widgets,
        }
