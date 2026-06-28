"""Connector registry. Phase 2 ships Trino only; later phases add
pyiceberg-direct and Oracle via the same shape: drop a Connector subclass
in this package, register it in CONNECTOR_REGISTRY.
"""

from __future__ import annotations

from dq_core.errors import ConfigError
from warehouse_validation.connectors.base import Connector
from warehouse_validation.connectors.trino import TrinoConnector


CONNECTOR_REGISTRY: dict[str, type[Connector]] = {
    "trino": TrinoConnector,
}


def get_connector(name: str) -> Connector:
    """Resolve a connector by name. Raises ConfigError on unknown names."""
    cls = CONNECTOR_REGISTRY.get(name)
    if cls is None:
        raise ConfigError(
            f"unknown connector {name!r}; known: {sorted(CONNECTOR_REGISTRY)}"
        )
    return cls()


__all__ = ["Connector", "CONNECTOR_REGISTRY", "get_connector"]
