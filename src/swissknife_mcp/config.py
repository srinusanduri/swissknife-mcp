"""Server configuration.

Sources, lowest to highest precedence:

1. Built-in defaults (see the models below).
2. ``config.yaml`` (path from ``--config``, ``$SWISSKNIFE_CONFIG`` or ``./config.yaml``).
   String values may reference environment variables: ``${VAR}`` or ``${VAR:-default}``.
3. Environment variables ``SWISSKNIFE_*`` (see ``_ENV_OVERRIDES``).
4. CLI flags (applied by ``__main__``).

Secrets live in ``.env`` (loaded into the process environment, never overriding
variables that are already set) and are read by toolsets via ``os.environ``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

ALL = "all"
CONFIG_ENV_VAR = "SWISSKNIFE_CONFIG"
DEFAULT_CONFIG_FILE = "config.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def split_csv(value: Any) -> list[str]:
    """Accept ``"a,b"``, ``["a", "b"]`` or ``None`` and return a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = [str(v) for v in value]
    return [item.strip() for item in items if item and item.strip()]


class ServerSettings(BaseModel):
    name: str = "swissknife-mcp"
    instructions: str | None = None
    transport: Literal["http", "stdio"] = "http"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    log_level: str = "INFO"
    show_banner: bool = False


class ToolsetSelection(BaseModel):
    include: list[str] = Field(default_factory=lambda: [ALL])
    exclude: list[str] = Field(default_factory=list)

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> list[str]:
        return split_csv(value)

    @property
    def includes_all(self) -> bool:
        return not self.include or ALL in self.include


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    toolsets: ToolsetSelection = Field(default_factory=ToolsetSelection)
    omit_tools: list[str] = Field(default_factory=list)
    # Free-form, per-toolset settings keyed by toolset name. Passed to the toolset as `self.config`.
    toolset_config: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("omit_tools", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> list[str]:
        return split_csv(value)

    @field_validator("toolset_config", mode="before")
    @classmethod
    def _none_to_dict(cls, value: Any) -> Any:
        if value is None:
            return {}
        return {k: (v or {}) for k, v in value.items()}

    def config_for(self, toolset_name: str) -> dict[str, Any]:
        return dict(self.toolset_config.get(toolset_name, {}))


def _interpolate(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


# env var -> (section, key); section None means top-level key.
_ENV_OVERRIDES: dict[str, tuple[str | None, str]] = {
    "SWISSKNIFE_TOOLSETS": ("toolsets", "include"),
    "SWISSKNIFE_EXCLUDE_TOOLSETS": ("toolsets", "exclude"),
    "SWISSKNIFE_OMIT_TOOLS": (None, "omit_tools"),
    "SWISSKNIFE_TRANSPORT": ("server", "transport"),
    "SWISSKNIFE_HOST": ("server", "host"),
    "SWISSKNIFE_PORT": ("server", "port"),
    "SWISSKNIFE_PATH": ("server", "path"),
    "SWISSKNIFE_LOG_LEVEL": ("server", "log_level"),
}


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is None or value == "":
            continue
        if section is None:
            raw[key] = value
        else:
            raw[section] = {**(raw.get(section) or {}), key: value}
    return raw


def resolve_config_path(config_path: str | Path | None) -> Path | None:
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return resolve_config_path(env_path)
    default = Path.cwd() / DEFAULT_CONFIG_FILE
    return default if default.is_file() else None


def load_settings(
    config_path: str | Path | None = None, env_file: str | Path | None = None
) -> Settings:
    """Load ``.env`` + ``config.yaml`` + ``SWISSKNIFE_*`` env overrides into ``Settings``."""
    # 1) .env -> process environment (explicit file, else cwd). Real env vars win.
    if env_file:
        load_dotenv(Path(env_file).expanduser(), override=False)
    else:
        load_dotenv(Path.cwd() / ".env", override=False)

    path = resolve_config_path(config_path)
    raw: dict[str, Any] = {}
    if path is not None:
        # Also pick up a .env that sits next to the config file (useful for stdio launches).
        if not env_file:
            load_dotenv(path.parent / ".env", override=False)
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Config file {path} must contain a YAML mapping at the top level")
        logger.debug("Loaded config from %s", path)

    raw = _apply_env_overrides(_interpolate(raw))
    return Settings.model_validate(raw)
