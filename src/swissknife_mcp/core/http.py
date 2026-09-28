"""Shared helpers for toolsets that call HTTP APIs."""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_ERROR_BODY = 2000


def raise_for_status(response: httpx.Response, service: str) -> None:
    """Convert non-2xx responses into a ``ToolError`` the LLM can read and act on."""
    if response.is_success:
        return
    body = response.text[:_MAX_ERROR_BODY] if response.text else ""
    hint = {
        401: "authentication failed - check API credentials",
        403: "forbidden - credentials lack the required permission/scope",
        404: "resource not found - check the id",
        429: "rate limited - retry later",
    }.get(response.status_code, "")
    msg = f"{service} API error {response.status_code}"
    if hint:
        msg += f" ({hint})"
    if body:
        msg += f": {body}"
    raise ToolError(msg)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    **kwargs: Any,
) -> Any:
    """Perform a request and return parsed JSON, mapping failures to ``ToolError``."""
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ToolError(f"{service} API request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"{service} API request failed: {exc}") from exc
    raise_for_status(response, service)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ToolError(f"{service} API returned non-JSON response: {response.text[:500]}") from exc
