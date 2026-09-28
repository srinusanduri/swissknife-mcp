# Datadog toolset

| | |
|---|---|
| **Toolset name** | `datadog` |
| **Source** | [`src/swissknife_mcp/tools/datadog.py`](../../src/swissknife_mcp/tools/datadog.py) |
| **Required env** | `DD_API_KEY`, `DD_APP_KEY` |
| **Optional env** | `DD_SITE` (default `datadoghq.com`) |
| **Tools** | `list_dashboards_usage`, `get_dashboard_usage`, `list_dashboards`, `get_dashboard`, `get_dashboard_queries` |

Tools for Datadog dashboards: find dashboards, get usage statistics (views, staleness,
widget counts, quality score) and pull the queries behind every widget.

All tools are read-only. Their MCP annotations are `readOnlyHint`, `idempotentHint` and
`openWorldHint`.

## Setup

### Credentials

Add the keys to `.env`, or export them in your shell. Exported shell variables take
precedence over `.env`.

```dotenv
DD_API_KEY=<api key>
DD_APP_KEY=<application key>
DD_SITE=datadoghq.com
```

- **API key**: Organization Settings → API Keys.
- **Application key**: Organization Settings → Application Keys. The key's user (or the
  scope, if the key is scoped) needs the **`dashboards_read`** permission.
- If `DD_API_KEY` or `DD_APP_KEY` is missing, the toolset is skipped at startup with a
  warning. `GET /toolsets` shows it under `skipped`.

### Datadog site

| Region | `DD_SITE` |
|---|---|
| US1 (default) | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU1 | `datadoghq.eu` |
| AP1 | `ap1.datadoghq.com` |
| US1-FED | `ddog-gov.com` |

The API base URL becomes `https://api.<site>`.

### config.yaml

```yaml
toolset_config:
  datadog:
    site: ${DD_SITE:-datadoghq.com}   # overrides DD_SITE
    timeout: 30                       # HTTP timeout in seconds
    # api_url: http://localhost:8080  # full base URL override (proxy / mock); wins over site
```

### Enable or disable

```bash
uv run swissknife-mcp --toolsets datadog                 # only datadog
uv run swissknife-mcp --exclude-toolsets datadog         # everything except datadog
uv run swissknife-mcp --omit-tools get_dashboard         # hide the raw JSON tool
```

Per client (HTTP): `http://127.0.0.1:8000/mcp?toolsets=datadog&omit_tools=get_dashboard`

## Tools

### `list_dashboards_usage`

Usage statistics for **all** dashboards in the org. Calls `GET /api/v2/dashboards/usage`.

| Argument | Type | Default | Description |
|---|---|---|---|
| `limit` | int (1-500) | `250` | Page size. |
| `offset` | int (>= 0) | `0` | Zero-based offset. |
| `edited_before` | ISO 8601 string | – | Only dashboards last edited **before** this time. Must include a timezone, e.g. `2025-04-26T00:00:00Z`. |
| `viewed_before` | ISO 8601 string | – | Only dashboards last viewed before this time. Includes dashboards that were never viewed. |
| `fetch_all` | bool | `false` | Follow pagination and return every page. |
| `max_results` | int | `5000` | Safety cap when `fetch_all=true`. |

Returns:

```json
{
  "count": 2,
  "total": 1234,
  "next_offset": 250,
  "dashboards": [
    {
      "id": "q5j-nti-fv6",
      "title": "My production overview",
      "author": {"handle": "jane.doe@example.com", "name": "Jane Doe"},
      "created_at": "2026-01-15T09:30:00.000Z",
      "edited_at": "2026-04-20T11:05:00.000Z",
      "viewed_at": "2026-05-01T14:22:10.000Z",
      "viewer": {"handle": "john.smith@example.com"},
      "total_views": 42,
      "total_views_by_type": {"in_app": 30, "embed": 12},
      "widget_count": 12,
      "widget_count_by_type": {"timeseries": 8, "query_value": 4},
      "teams": ["sre"],
      "dashboard_quality_score": 0.85,
      "org_id": 100
    }
  ]
}
```

`next_offset` is `null` when there are no more pages, or after a complete `fetch_all`.

Example prompts:
- "Which dashboards haven't been viewed since 2025-06-01?" uses `viewed_before`.
- "Top 10 most viewed dashboards" uses `fetch_all=true`, then sorts by `total_views`.

### `get_dashboard_usage`

Usage statistics for one dashboard. Calls `GET /api/v2/dashboards/{dashboard_id}/usage`.

| Argument | Type | Description |
|---|---|---|
| `dashboard_id` | string | Dashboard id, e.g. `abc-def-ghi`. It's in the dashboard URL: `/dashboard/<id>/...`. |

Returns a single object with the same fields as one entry of `list_dashboards_usage`.

### `list_dashboards`

List dashboards so you can find their ids. Calls `GET /api/v1/dashboard`.

| Argument | Type | Default | Description |
|---|---|---|---|
| `title_contains` | string | – | Case-insensitive substring match on the title. When set, the tool fetches the full list and filters it locally. |
| `shared_only` | bool | `false` | Only shared dashboards. |
| `include_deleted` | bool | `false` | Only deleted dashboards. |
| `count` | int (1-1000) | `100` | Max dashboards to return. |
| `start` | int | `0` | Offset. |

Returns:

```json
{
  "count": 1,
  "total_matching": 1,
  "dashboards": [
    {
      "id": "abc-def-ghi",
      "title": "Prod overview",
      "url": "/dashboard/abc-def-ghi/prod-overview",
      "layout_type": "ordered",
      "author_handle": "jane.doe@example.com",
      "created_at": "2025-01-01T00:00:00.000Z",
      "modified_at": "2026-04-20T11:05:00.000Z",
      "is_read_only": false
    }
  ]
}
```

`total_matching` is only populated when `title_contains` is used.

### `get_dashboard`

The full, raw dashboard definition: widgets, layout and template variables. Calls
`GET /api/v1/dashboard/{dashboard_id}`.

| Argument | Type | Description |
|---|---|---|
| `dashboard_id` | string | Dashboard id. |

Large dashboards return a lot of JSON. Use `get_dashboard_queries` when you only need
queries, or hide this tool with `omit_tools`.

### `get_dashboard_queries`

Every query used by each widget (panel) of a dashboard. It fetches
`GET /api/v1/dashboard/{dashboard_id}` and extracts the queries.

| Argument | Type | Default | Description |
|---|---|---|---|
| `dashboard_id` | string | – | Dashboard id. |
| `include_empty_widgets` | bool | `false` | Also list widgets that have no queries (notes, images, iframes, ...). |

What it handles:

| Where queries live | Example widgets | Output |
|---|---|---|
| Legacy metric string `requests[].q` | timeseries, toplist, query_value | `{"data_source": "metrics", "query": "avg:system.cpu.user{*}"}` |
| Formula style `requests[].queries[]` + `formulas[]` | most modern widgets | Each query keeps its fields: `name`, `data_source`, `query`, plus `aggregator`, `compute`, `group_by`, `indexes` and so on. Formulas go in `formulas`. |
| Legacy product queries `log_query`, `apm_query`, `rum_query`, `event_query`, `security_query`, `network_query`, `audit_query`, `profile_metrics_query`, `process_query`, `apm_stats_query` | older log/APM widgets | `data_source` is the product name (e.g. `log`). The query string is taken from `search.query`. |
| Single request `query` object | list_stream, funnel, slo_list, cohort, point_plot | Normalized query |
| Widget-level `query` string | log_stream, event_stream, event_timeline, monitor_summary | `data_source` is `logs`, `events` or `monitors` |
| Dict-shaped `requests` | hostmap (`fill`/`size`), scatterplot (`x`/`y`/`table`) | The `request` field shows which slot, e.g. `fill` |
| Referenced objects instead of queries | slo, alert_graph, check_status, service_summary, powerpack, ... | `references`, e.g. `{"slo_id": "..."}` |
| Containers | group, powerpack (inline widgets), split_graph (`source_widget_definition`) | Recursed. Titles of the parents are listed in `group_path`. |

Returns:

```json
{
  "dashboard_id": "abc-def-ghi",
  "title": "Prod overview",
  "url": "/dashboard/abc-def-ghi/prod-overview",
  "layout_type": "ordered",
  "template_variables": [{"name": "env", "prefix": "env", "default": "prod"}],
  "widget_count": 2,
  "query_count": 3,
  "widgets": [
    {
      "widget_id": 1,
      "widget_type": "timeseries",
      "title": "CPU",
      "group_path": [],
      "queries": [
        {"data_source": "metrics", "query": "avg:system.cpu.user{$env} by {host}", "request": "0"}
      ]
    },
    {
      "widget_id": 21,
      "widget_type": "query_value",
      "title": "Error rate",
      "group_path": ["APM"],
      "queries": [
        {"name": "errors", "data_source": "metrics",
         "query": "sum:trace.http.request.errors{$env}.as_count()", "request": "0"},
        {"name": "logs", "data_source": "logs", "query": "status:error",
         "indexes": ["main"], "compute": {"aggregation": "count"}, "request": "0"}
      ],
      "formulas": [{"formula": "errors / logs", "request": "0"}]
    }
  ]
}
```

`request` is the index (or dict key) of the widget request the query came from. Use it to
match formulas with their queries.

## Behaviour and limitations

- **Retries**: HTTP 429, 502, 503 and 504 are retried up to 3 times. The wait follows
  Datadog's `X-RateLimit-Reset` header (capped at 30s), or backs off exponentially.
- **Errors**: non-2xx responses come back as tool errors (`isError: true`) with the status,
  a hint (401 auth, 403 permission, 404 not found, 429 rate limit) and the start of the
  response body.
- **Usage endpoints are in preview.** Datadog marks `/api/v2/dashboards/usage` and
  `/api/v2/dashboards/{id}/usage` as preview/unstable, so they may change.
- **View counts** come from Real User Monitoring (RUM). They are `null` or `0` in orgs
  without RUM, refresh once per day, and only include views since January 2025.
- `list_dashboards` with `title_contains` fetches the whole dashboard list, which can be
  slow for very large orgs.
- Powerpacks that reference a shared powerpack by `powerpack_id` only appear as a reference.
  Their queries aren't resolved.

## Manual test

With the server running and your keys set (see
[Manual testing](../../README.md#manual-testing-of-the-mcp-http-api) in the README):

```bash
uv run fastmcp call "http://127.0.0.1:8000/mcp" list_dashboards_usage limit=5
uv run fastmcp call "http://127.0.0.1:8000/mcp" list_dashboards title_contains=prod
uv run fastmcp call "http://127.0.0.1:8000/mcp" get_dashboard_queries dashboard_id=abc-def-ghi
```

To check your credentials directly against Datadog, without the MCP server:

```bash
curl -s "https://api.${DD_SITE:-datadoghq.com}/api/v2/dashboards/usage?page[limit]=1" \
  -H "Accept: application/json" \
  -H "DD-API-KEY: ${DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" | jq
```
