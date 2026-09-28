"""Auto-discovery of toolset modules.

Imports every module (recursively) under a package - by default ``swissknife_mcp.tools`` -
so that their ``@toolset`` decorators run and register themselves. Modules whose name
starts with ``_`` are skipped, which is handy for shared helpers or disabled drafts.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field

from swissknife_mcp.core.toolset import ToolsetRegistry, registry

logger = logging.getLogger(__name__)

DEFAULT_TOOLS_PACKAGE = "swissknife_mcp.tools"


@dataclass
class DiscoveryResult:
    imported: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def discover_toolsets(
    packages: list[str] | tuple[str, ...] = (DEFAULT_TOOLS_PACKAGE,),
    *,
    target_registry: ToolsetRegistry = registry,
    strict: bool = False,
) -> DiscoveryResult:
    """Import all toolset modules in ``packages``.

    A module that fails to import (e.g. missing optional dependency) is logged and skipped
    unless ``strict`` is True, so one broken toolset never takes the whole server down.
    """
    result = DiscoveryResult()
    for package_name in packages:
        package = importlib.import_module(package_name)
        search_path = getattr(package, "__path__", None)
        if search_path is None:
            continue
        for module_info in pkgutil.walk_packages(search_path, prefix=f"{package_name}."):
            leaf = module_info.name.rsplit(".", 1)[-1]
            if leaf.startswith("_"):
                continue
            try:
                importlib.import_module(module_info.name)
                result.imported.append(module_info.name)
            except Exception as exc:  # noqa: BLE001 - isolate faulty plugins
                if strict:
                    raise
                result.failed[module_info.name] = f"{type(exc).__name__}: {exc}"
                logger.warning("Failed to import toolset module %s: %s", module_info.name, exc)
    logger.debug("Discovered toolsets %s from modules %s", target_registry.names(), result.imported)
    return result
