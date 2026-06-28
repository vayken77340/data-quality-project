"""Gold-rule discovery + sidecar parsing.

Each rule is a pair of files under `epics/<E>/rules/gold/`:
  <name>.sql   -- one SQL statement that returns a single scalar count.
                  Read verbatim and handed to connector.execute_scalar.
  <name>.yaml  -- metadata sidecar:
                    name:           (required; must equal basename)
                    description:    (required; non-empty)
                    table:          (required; logical table the rule
                                     scores against, used to group
                                     violations in the report)
                    severity:       (optional; "error" | "warning";
                                     default "error")
                    expected_count: (optional; non-negative int; default 0)
                  Unknown top-level keys are rejected so typos fail loudly.

Discovery returns rules sorted by name. A directory that doesn't exist
or is empty returns []. Any inconsistency (orphan files, malformed
sidecar) raises ConfigError; orphans are collected and reported
together so the operator sees the full picture in one run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dq_core.errors import ConfigError
from dq_core.yaml_io import load_yaml_mapping


RULES_DIR_RELPATH = Path("rules") / "gold"

_ALLOWED_SEVERITIES = frozenset({"error", "warning"})
_REQUIRED_KEYS = frozenset({"name", "description", "table"})
_OPTIONAL_KEYS = frozenset({"severity", "expected_count"})
_KNOWN_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS


@dataclass(frozen=True)
class GoldRule:
    name: str
    sql: str
    description: str
    table: str
    severity: str          # "error" | "warning"
    expected_count: int


def discover_rules(rules_dir: Path) -> list[GoldRule]:
    """Find every <name>.sql/<name>.yaml pair under `rules_dir`,
    parse each, return them sorted by name. Missing or empty
    directory returns []. Orphan files raise a single ConfigError
    listing all offenders together."""
    if not rules_dir.is_dir():
        return []

    sql_names = {p.stem for p in rules_dir.glob("*.sql")}
    yaml_names = {p.stem for p in rules_dir.glob("*.yaml")}

    orphan_sql = sorted(sql_names - yaml_names)
    orphan_yaml = sorted(yaml_names - sql_names)
    if orphan_sql or orphan_yaml:
        msgs: list[str] = []
        if orphan_sql:
            msgs.append(
                f".sql without matching .yaml: {orphan_sql}"
            )
        if orphan_yaml:
            msgs.append(
                f".yaml without matching .sql: {orphan_yaml}"
            )
        raise ConfigError(
            f"{rules_dir}: orphan gold rule files; "
            + "; ".join(msgs)
        )

    return [
        _build_rule(rules_dir, name)
        for name in sorted(sql_names)
    ]


def load_rule(rules_dir: Path, name: str) -> GoldRule:
    """Load a single rule by name. Raises ConfigError if either of
    the pair is missing."""
    sql_path = rules_dir / f"{name}.sql"
    yaml_path = rules_dir / f"{name}.yaml"
    missing = [p for p in (sql_path, yaml_path) if not p.is_file()]
    if missing:
        raise ConfigError(
            f"{rules_dir}: gold rule {name!r} is missing file(s): "
            f"{[str(p) for p in missing]}"
        )
    return _build_rule(rules_dir, name)


def _build_rule(rules_dir: Path, name: str) -> GoldRule:
    sql_path = rules_dir / f"{name}.sql"
    yaml_path = rules_dir / f"{name}.yaml"

    sql = sql_path.read_text(encoding="utf-8")
    if not sql.strip():
        raise ConfigError(f"{sql_path}: SQL file is empty")
    if "placeholder_catalog" in sql or "placeholder_schema" in sql:
        raise ConfigError(
            f"{sql_path}: SQL still references placeholder_catalog / "
            f"placeholder_schema. Update the FROM clause to your real "
            f"warehouse coordinates before running validate-gold."
        )

    sidecar = load_yaml_mapping(yaml_path, what="gold rule sidecar")
    return _parse_sidecar(name=name, yaml_path=yaml_path, sidecar=sidecar, sql=sql)


def _parse_sidecar(
    *,
    name: str,
    yaml_path: Path,
    sidecar: dict[str, Any],
    sql: str,
) -> GoldRule:
    unknown = sorted(set(sidecar) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"{yaml_path}: unknown top-level key(s) {unknown}; "
            f"allowed: {sorted(_KNOWN_KEYS)}"
        )

    missing = sorted(_REQUIRED_KEYS - set(sidecar))
    if missing:
        raise ConfigError(
            f"{yaml_path}: missing required key(s) {missing}"
        )

    declared_name = sidecar["name"]
    if not isinstance(declared_name, str) or not declared_name:
        raise ConfigError(f"{yaml_path}: 'name' must be a non-empty string")
    if declared_name != name:
        raise ConfigError(
            f"{yaml_path}: 'name' field {declared_name!r} does not "
            f"match file basename {name!r}"
        )

    description = sidecar["description"]
    if not isinstance(description, str) or not description.strip():
        raise ConfigError(
            f"{yaml_path}: 'description' must be a non-empty string"
        )

    table = sidecar["table"]
    if not isinstance(table, str) or not table:
        raise ConfigError(f"{yaml_path}: 'table' must be a non-empty string")

    severity = sidecar.get("severity", "error")
    if severity not in _ALLOWED_SEVERITIES:
        raise ConfigError(
            f"{yaml_path}: 'severity' must be one of "
            f"{sorted(_ALLOWED_SEVERITIES)} (got {severity!r})"
        )

    expected_count = sidecar.get("expected_count", 0)
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise ConfigError(
            f"{yaml_path}: 'expected_count' must be an integer "
            f"(got {expected_count!r})"
        )
    if expected_count < 0:
        raise ConfigError(
            f"{yaml_path}: 'expected_count' must be non-negative "
            f"(got {expected_count})"
        )

    return GoldRule(
        name=name,
        sql=sql,
        description=description,
        table=table,
        severity=severity,
        expected_count=expected_count,
    )
