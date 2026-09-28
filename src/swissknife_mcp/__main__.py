"""CLI entry point: ``swissknife-mcp`` / ``python -m swissknife_mcp``."""

from __future__ import annotations

import argparse
import logging
import sys

from swissknife_mcp import __version__
from swissknife_mcp.config import Settings, load_settings, split_csv


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="swissknife-mcp", description="Generic multi-purpose MCP server."
    )
    parser.add_argument("-c", "--config", help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--env-file", help="Path to .env file (default: ./.env)")
    parser.add_argument("--transport", choices=["http", "stdio"], help="Override transport")
    parser.add_argument("--host", help="Override HTTP host")
    parser.add_argument("--port", type=int, help="Override HTTP port")
    parser.add_argument("--toolsets", help="Comma-separated toolsets to load ('all' for every one)")
    parser.add_argument("--exclude-toolsets", help="Comma-separated toolsets to skip")
    parser.add_argument("--omit-tools", help="Comma-separated tool names to never expose")
    parser.add_argument(
        "--list", action="store_true", help="Print discovered toolsets/tools and exit"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    server = settings.server.model_copy(
        update={
            k: v
            for k, v in {"transport": args.transport, "host": args.host, "port": args.port}.items()
            if v is not None
        }
    )
    toolsets = settings.toolsets.model_copy(
        update={
            k: split_csv(v)
            for k, v in {"include": args.toolsets, "exclude": args.exclude_toolsets}.items()
            if v is not None
        }
    )
    omit = split_csv(args.omit_tools) if args.omit_tools is not None else settings.omit_tools
    return settings.model_copy(update={"server": server, "toolsets": toolsets, "omit_tools": omit})


def _configure_logging(level: str) -> None:
    # Always log to stderr: stdout is the protocol channel for the stdio transport.
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = _apply_cli_overrides(load_settings(args.config, args.env_file), args)
    _configure_logging(settings.server.log_level)

    from swissknife_mcp.server import build_server  # after logging is configured

    mcp = build_server(settings)
    state = mcp.swissknife_state  # type: ignore[attr-defined]

    if args.list:
        for name, lt in state.toolsets.items():
            print(f"{name}: {', '.join(lt.tools) or '(no tools)'}")
        for name, reason in state.skipped.items():
            print(f"{name}: SKIPPED ({reason})")
        return

    srv = settings.server
    if srv.transport == "stdio":
        mcp.run(transport="stdio", show_banner=srv.show_banner)
    else:
        mcp.run(
            transport="http",
            host=srv.host,
            port=srv.port,
            path=srv.path,
            log_level=srv.log_level.lower(),
            show_banner=srv.show_banner,
        )


if __name__ == "__main__":
    main()
