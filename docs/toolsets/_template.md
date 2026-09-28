# <Name> toolset

<!-- Copy this file to docs/toolsets/<toolset-name>.md and add a row to the
     "Toolsets" table in the README. -->

| | |
|---|---|
| **Toolset name** | `<name>` |
| **Source** | [`src/swissknife_mcp/tools/<name>.py`](../../src/swissknife_mcp/tools/<name>.py) |
| **Required env** | `<VAR>` |
| **Optional env** | – |
| **Tools** | `<tool_a>`, `<tool_b>` |

One or two sentences on what this toolset is for.

## Setup

### Credentials

```dotenv
<VAR>=
```

Where to get the credentials and which permissions or scopes they need.

### config.yaml

```yaml
toolset_config:
  <name>:
    some_setting: value
```

## Tools

### `<tool_a>`

What it does, and which API it calls.

| Argument | Type | Default | Description |
|---|---|---|---|
| `arg` | string | – | ... |

Returns:

```json
{}
```

## Behaviour and limitations

- Retries, pagination, rate limits, known gaps.

## Manual test

```bash
uv run fastmcp call "http://127.0.0.1:8000/mcp" <tool_a> arg=value
```
