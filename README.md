# swissknife-mcp

A generic, pluggable, multi-purpose [MCP](https://modelcontextprotocol.io) server built on
[FastMCP 4](https://gofastmcp.com). Tools are grouped into **toolsets** (Datadog, data lake,
mail, ...). Adding a toolset = dropping **one file** into `src/swissknife_mcp/tools/`.

## Setup

Requires **Python 3.11+** (`python3 --version`). All dependencies are installed into a
project-local virtual environment at **`.venv/`**. It is a hidden directory, so plain `ls`
won't show it. Use `ls -a`.

### Option 1: uv (recommended)

```bash
cd /Users/SSANDURI/Workspace/Practice/swissknife-mcp
brew install uv                  # once

uv venv                          # create .venv  (uses a Python >= 3.11 from your system)
uv sync                          # install the project + dev deps from uv.lock into .venv
source .venv/bin/activate        # activate it in this shell

which python                     # -> .../swissknife-mcp/.venv/bin/python
swissknife-mcp --version         # -> swissknife-mcp 0.1.0
```

`uv sync` creates `.venv` on its own if it's missing, so `uv venv` is optional. If `.venv`
already exists, `uv venv` stops with "A virtual environment already exists". Skip it, or
use `uv venv --clear` to recreate it. `uv sync` **does not activate** the environment. After `uv sync`, `which python3` still prints
`/opt/homebrew/bin/python3` until you run `source .venv/bin/activate`. You have two choices:

- **Activate** once per terminal: `source .venv/bin/activate`. Then run `python`,
  `swissknife-mcp`, `pytest` and `fastmcp` directly. Leave with `deactivate`.
- **Don't activate**: prefix commands with `uv run`, e.g. `uv run swissknife-mcp`. uv runs
  them inside `.venv` for you.

To pin a specific interpreter: `uv venv --python 3.12`. To rebuild from scratch:
`rm -rf .venv && uv sync`.

Troubleshooting:

| Symptom | Cause / fix |
|---|---|
| `ls` doesn't show `.venv` | It's a hidden directory. Use `ls -a` or `ls .venv/bin`. |
| `which python3` is still `/opt/homebrew/bin/python3` after `uv sync` | The venv isn't activated. Run `source .venv/bin/activate`, or use `uv run ...`. |
| `swissknife-mcp: command not found` | Same cause: activate the venv, or use `uv run swissknife-mcp` or `.venv/bin/swissknife-mcp`. |
| `ModuleNotFoundError: No module named 'swissknife_mcp'` | The editable install is stale. Run `uv sync --reinstall-package swissknife-mcp`, or `pip install -e .`. |
| IDE (PyCharm/VS Code) shows unresolved imports | Set the project interpreter to `.venv/bin/python`. |

### Option 2: plain Python venv + pip (no uv)

```bash
cd /Users/SSANDURI/Workspace/Practice/swissknife-mcp

python3 -m venv .venv            # create .venv
source .venv/bin/activate        # activate it
python -m pip install --upgrade pip   # pip >= 25.1 is needed for --group
pip install -e .                 # install the project (editable) + runtime deps
pip install --group dev          # dev tools: pytest, respx, ruff (optional)

which python                     # -> .../swissknife-mcp/.venv/bin/python
```

With pip, versions are resolved fresh from `pyproject.toml`. They are not pinned by
`uv.lock`.

### Configure secrets

```bash
cp .env.example .env             # then fill in DD_API_KEY, DD_APP_KEY, DD_SITE
```

Variables already exported in your shell take precedence over `.env`.

## Run

With `.venv` activated (drop the `uv run` prefix), or without activating (keep `uv run`):

```bash
uv run swissknife-mcp --list               # show discovered toolsets/tools (and skipped ones)
uv run swissknife-mcp                      # streamable HTTP on http://127.0.0.1:8000/mcp
uv run swissknife-mcp --port 9000 --toolsets datadog --omit-tools get_dashboard
uv run swissknife-mcp --transport stdio    # for agents that launch the server themselves
uv run swissknife-mcp -c /path/to/config.yaml --env-file /path/to/.env
```

Without uv or activation, call the venv's executable directly:
`.venv/bin/swissknife-mcp` or `.venv/bin/python -m swissknife_mcp`.

Also available: `GET /health` and `GET /toolsets` (what was loaded / skipped and why).
To call tools by hand, see [Manual testing](#manual-testing-of-the-mcp-http-api). Each
toolset has its own page under [docs/toolsets](#toolsets).

## Selecting toolsets and tools

### Server side (authoritative)

Precedence: CLI flags > `SWISSKNIFE_*` env vars > `config.yaml` > defaults.

| What               | config.yaml          | env var                       | CLI                  |
|--------------------|----------------------|-------------------------------|----------------------|
| toolsets to load   | `toolsets.include`   | `SWISSKNIFE_TOOLSETS`         | `--toolsets`         |
| toolsets to skip   | `toolsets.exclude`   | `SWISSKNIFE_EXCLUDE_TOOLSETS` | `--exclude-toolsets` |
| tools to hide      | `omit_tools`         | `SWISSKNIFE_OMIT_TOOLS`       | `--omit-tools`       |

Default is `include: [all]`. A toolset whose `required_env` vars are missing is skipped with
a warning. If a toolset's `setup()` fails, its tools are disabled and the rest keep working.

### Client side (per connection, HTTP only)

Same idea as the Datadog MCP server URL:

```
http://127.0.0.1:8000/mcp?toolsets=datadog&omit_tools=get_dashboard,list_dashboards
```

Headers `X-Swissknife-Toolsets` / `X-Swissknife-Omit-Tools` work too. Client filters can only
**narrow** what the server exposes. Filtered tools vanish from `tools/list`, and calling one
returns `Unknown tool`.

### Agent configuration examples

```jsonc
// HTTP (run the server separately)
{ "mcpServers": { "swissknife": { "url": "http://127.0.0.1:8000/mcp?toolsets=datadog" } } }

// stdio via uv (the agent launches the server)
{ "mcpServers": { "swissknife": {
    "command": "uv",
    "args": ["run", "--directory", "/Users/SSANDURI/Workspace/Practice/swissknife-mcp",
             "swissknife-mcp", "--transport", "stdio", "--toolsets", "datadog"] } } }

// stdio via the .venv executable directly (no uv needed at runtime)
{ "mcpServers": { "swissknife": {
    "command": "/Users/SSANDURI/Workspace/Practice/swissknife-mcp/.venv/bin/swissknife-mcp",
    "args": ["--transport", "stdio",
             "-c", "/Users/SSANDURI/Workspace/Practice/swissknife-mcp/config.yaml",
             "--env-file", "/Users/SSANDURI/Workspace/Practice/swissknife-mcp/.env"] } } }
```

For stdio, agents often start the server from a different working directory. Pass
`-c`/`--env-file` with absolute paths, as in the second example, so the config and secrets
are found. With `uv run --directory`, the working directory is already the project.

## Adding a toolset

Create `src/swissknife_mcp/tools/<name>.py`. Nothing else changes:

```python
from typing import Annotated
import httpx
from pydantic import Field
from swissknife_mcp.core import BaseToolset, request_json, tool, toolset


@toolset(name="weather", description="Weather lookups", required_env=["WEATHER_API_KEY"])
class WeatherToolset(BaseToolset):

    async def setup(self) -> None:                 # optional; runs once at startup
        self.client = httpx.AsyncClient(
            base_url=self.config.get("base_url", "https://api.weather.example"),
            headers={"Authorization": f"Bearer {self.env('WEATHER_API_KEY')}"},
        )

    async def teardown(self) -> None:              # optional; runs once at shutdown
        await self.client.aclose()

    @tool(read_only=True, open_world=True)
    async def get_forecast(
        self,
        city: Annotated[str, Field(description="City name")],
        days: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> dict:
        """Get the weather forecast for a city."""   # becomes the tool description
        return await request_json(self.client, "GET", "/forecast",
                                  params={"city": city, "days": days}, service="Weather")
```

The contract:

- `@toolset(name=..., description=..., required_env=[...])` goes on a `BaseToolset`
  subclass. The name is what you use in config and `?toolsets=`.
- `@tool` / `@tool(name=, title=, description=, tags=, read_only=, destructive=, idempotent=, open_world=, timeout=)`
  goes on methods. The method name is the tool name, the docstring is the description, and
  the type hints are the input schema (use `Annotated[..., Field(...)]` for docs/validation).
- `self.config` holds `toolset_config.<name>` from `config.yaml`, `self.env(VAR)` reads
  secrets from `.env` or the environment, and `self.logger` is a per-toolset logger.
- Raise `fastmcp.exceptions.ToolError` for errors the LLM should see.
  `core.request_json` / `raise_for_status` map HTTP failures for you.
- Tool names must be unique across toolsets (checked at startup). Modules whose name starts
  with `_` are ignored, which is useful for shared helpers.

When you add a toolset, also add its documentation: copy
[`docs/toolsets/_template.md`](docs/toolsets/_template.md) to `docs/toolsets/<name>.md` and add
a row to the table below.

## Toolsets

| Toolset | Description | Tools | Docs |
|---|---|---|---|
| `datadog` | Datadog dashboards: list, definitions, usage statistics, widget queries | `list_dashboards_usage`, `get_dashboard_usage`, `list_dashboards`, `get_dashboard`, `get_dashboard_queries` | [docs/toolsets/datadog.md](docs/toolsets/datadog.md) |

## Manual testing of the MCP HTTP API

Start the server in one terminal:

```bash
uv run swissknife-mcp            # http://127.0.0.1:8000/mcp   (or: source .venv/bin/activate && swissknife-mcp)
```

### Server info endpoints (plain HTTP, no MCP)

```bash
curl -s http://127.0.0.1:8000/health   | jq   # {"status":"ok","version":"0.1.0"}
curl -s http://127.0.0.1:8000/toolsets | jq   # loaded toolsets + tools, skipped toolsets + reason
```

### Option A: FastMCP CLI (easiest)

The `fastmcp` CLI comes with the project dependencies. It handles the session for you.

```bash
# list tools (URL filters work here too)
uv run fastmcp list "http://127.0.0.1:8000/mcp"
uv run fastmcp list "http://127.0.0.1:8000/mcp?toolsets=datadog&omit_tools=get_dashboard"

# call a tool: key=value arguments, or a JSON object
uv run fastmcp call "http://127.0.0.1:8000/mcp" list_dashboards_usage limit=5
uv run fastmcp call "http://127.0.0.1:8000/mcp" list_dashboards_usage '{"limit": 5, "viewed_before": "2025-06-01T00:00:00Z"}'
```

You'll see a `UserWarning: Using in-memory token storage` line. It comes from FastMCP's
OAuth client code and is harmless, because this server doesn't use auth.

If you prefer a UI and have Node installed, the MCP Inspector works too:
`npx @modelcontextprotocol/inspector`. Choose transport "Streamable HTTP" and URL
`http://127.0.0.1:8000/mcp`.

### Option B: raw curl with a session (see what's on the wire)

MCP over Streamable HTTP is JSON-RPC 2.0 sent with `POST /mcp`. The flow is:

1. **`initialize`**: the server creates a session and returns its id in the
   **`Mcp-Session-Id` response header**.
2. **`notifications/initialized`**: tells the server the client is ready (returns `202`).
3. **Requests** (`tools/list`, `tools/call`, `ping`, ...), each sending the
   `Mcp-Session-Id` header.
4. **`DELETE /mcp`** with the session header ends the session.

Every POST needs `Content-Type: application/json` and
`Accept: application/json, text/event-stream`. Responses come back as Server-Sent Events:
an `event: message` line followed by `data: {json}`. That's why the helper below keeps only
the `data:` line.

These commands work in zsh and bash:

```bash
MCP_URL="http://127.0.0.1:8000/mcp"

# 1) Create a session and capture its id from the response header
SID=$(curl -s -D - -o /dev/null -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | awk 'tolower($1)=="mcp-session-id:" {print $2}' | tr -d '\r')
echo "session: $SID"

# Helper: mcp '<json-rpc body>' ['<query string>']
mcp() {
  curl -s -X POST "${MCP_URL}${2:+?$2}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SID" \
    -H "MCP-Protocol-Version: 2025-06-18" \
    -d "$1" | sed -n 's/^data: //p'
}

# 2) Finish the handshake (no response body, HTTP 202)
mcp '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3) Use the session
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | jq '.result.tools[].name'

mcp '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_dashboards_usage","arguments":{"limit":5}}}' \
  | jq '.result.structuredContent'

mcp '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_dashboard_queries","arguments":{"dashboard_id":"abc-def-ghi"}}}' \
  | jq '.result.structuredContent.widgets[] | {title, queries: [.queries[].query]}'

# Toolset / tool filters: pass them as the query string
mcp '{"jsonrpc":"2.0","id":5,"method":"tools/list"}' 'toolsets=datadog&omit_tools=get_dashboard' \
  | jq '.result.tools[].name'

# See a tool's input schema
mcp '{"jsonrpc":"2.0","id":6,"method":"tools/list"}' \
  | jq '.result.tools[] | select(.name=="list_dashboards_usage") | .inputSchema'

mcp '{"jsonrpc":"2.0","id":7,"method":"ping"}'          # {"jsonrpc":"2.0","id":7,"result":{}}

# 4) End the session
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "$MCP_URL" -H "Mcp-Session-Id: $SID"   # 200
```

Things to know:

- **Filters are read on every request**, not stored in the session. `?toolsets=` and
  `?omit_tools=` must be on each request you want filtered. MCP clients do this
  automatically because they always use the configured URL. The
  `X-Swissknife-Toolsets` / `X-Swissknife-Omit-Tools` headers work the same way.
- **Tool results**: `result.structuredContent` holds the JSON the tool returned, and
  `result.content[0].text` holds the same data as text. If a tool fails (for example a
  Datadog 404, or calling a filtered-out tool) the response is still a normal result, with
  `"isError": true` and the message in `content[0].text`.
- **Session errors**: no `Mcp-Session-Id` returns `400 Bad Request: Missing session ID`. An
  unknown or deleted session returns `404`. Restarting the server also invalidates
  sessions, so run `initialize` again.
- **Request ids**: the `id` values only need to be unique within the session.
- Add `-i` to `curl` (and drop the `sed`) to see the raw response headers and SSE framing.

## Development

```bash
uv run pytest            # tests (Datadog HTTP mocked with respx)
uv run ruff check src tests && uv run ruff format src tests

# same with an activated venv (source .venv/bin/activate)
pytest
ruff check src tests && ruff format src tests
```

After adding a dependency to `pyproject.toml`, run `uv sync`, or `pip install -e .` if you
set up with pip. New toolset files don't need a reinstall, because the project is installed
in editable mode.

Layout:

```
src/swissknife_mcp/
  __main__.py        CLI
  config.py          config.yaml + .env + env overrides
  server.py          discovery -> selection -> registration -> lifespan -> middleware
  core/toolset.py    BaseToolset, @toolset, @tool, registry
  core/discovery.py  auto-imports every module in swissknife_mcp.tools
  core/filtering.py  selection rules + per-request ?toolsets=/?omit_tools= middleware
  core/http.py       HTTP error -> ToolError helpers
  tools/             <- toolset plugins (datadog.py, ...)
docs/toolsets/       <- one doc per toolset (datadog.md, _template.md)
```
