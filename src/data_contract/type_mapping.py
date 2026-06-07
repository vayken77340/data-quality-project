from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import yaml

from data_contract.errors import ConfigError, RejectionError


class Type(str, Enum):
    """Canonical types carried in generated contract YAMLs. Oracle-flavored
    because the source specs target Oracle. Aliases (`string`, `numeric`,
    `Date horodatée`, etc.) resolve to these canonicals via configs/types.yaml.
    """
    VARCHAR = "varchar"
    INTEGER = "integer"
    DOUBLE = "double"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _MappingEntry:
    canonical: Type
    aliases: tuple[str, ...]
    parameters: tuple[str, ...]  # () | ("max_length",) | ("precision","scale")


@dataclass(frozen=True)
class ParsedType:
    type: Type
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None


@dataclass
class TypeRegistry:
    entries: list[_MappingEntry] = field(default_factory=list)
    _alias_index: dict[str, _MappingEntry] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for entry in self.entries:
            for alias in entry.aliases:
                key = _normalize_type_string(alias)
                if key in self._alias_index:
                    raise ConfigError(f"type registry: alias '{alias}' is declared twice")
                self._alias_index[key] = entry

    def lookup(self, base: str) -> _MappingEntry | None:
        return self._alias_index.get(_normalize_type_string(base))


_PAREN_RE = re.compile(r"^(?P<base>[^()]+)\s*\(\s*(?P<args>[^()]*)\s*\)\s*$")
_WS = re.compile(r"\s+")


def _normalize_type_string(s: str) -> str:
    return _WS.sub("", str(s).strip().lower())


def load_type_registry(path: Path) -> TypeRegistry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings = raw.get("mappings")
    if not isinstance(mappings, list):
        raise ConfigError(f"{path}: expected top-level 'mappings' list")

    entries: list[_MappingEntry] = []
    valid_canonicals = {t.value for t in Type if t is not Type.UNKNOWN}
    for i, item in enumerate(mappings):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: mappings[{i}] must be a mapping")
        canonical = item.get("canonical")
        if canonical not in valid_canonicals:
            raise ConfigError(
                f"{path}: mappings[{i}].canonical must be one of {sorted(valid_canonicals)}, got {canonical!r}"
            )
        aliases = item.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            raise ConfigError(f"{path}: mappings[{i}].aliases must be a non-empty list")

        parameter = item.get("parameter")
        parameters = item.get("parameters")
        if parameter is not None and parameters is not None:
            raise ConfigError(f"{path}: mappings[{i}] cannot have both 'parameter' and 'parameters'")
        if parameter is not None:
            params = (str(parameter),)
        elif parameters is not None:
            if not isinstance(parameters, list):
                raise ConfigError(f"{path}: mappings[{i}].parameters must be a list")
            params = tuple(str(p) for p in parameters)
        else:
            params = ()

        entries.append(
            _MappingEntry(
                canonical=Type(canonical),
                aliases=tuple(str(a) for a in aliases),
                parameters=params,
            )
        )
    return TypeRegistry(entries=entries)


def parse_type(raw: str | None, registry: TypeRegistry, *, sheet_row: int) -> tuple[ParsedType | None, RejectionError | None]:
    """Parse a raw spec type string.

    Returns (ParsedType, None) on success, or (None, RejectionError) on unknown type.
    """
    if raw is None or str(raw).strip() == "":
        # Caller decides whether empty is a mandatory violation; here we just refuse to parse.
        return None, RejectionError(
            kind="unknown_type",
            sheet_row=sheet_row,
            value=raw,
            message="type value is empty",
        )

    s = str(raw).strip()
    m = _PAREN_RE.match(s)
    if m:
        base = m.group("base")
        raw_args = [a.strip() for a in m.group("args").split(",") if a.strip() != ""]
    else:
        base = s
        raw_args = []

    entry = registry.lookup(base)
    if entry is None:
        return None, RejectionError(
            kind="unknown_type",
            sheet_row=sheet_row,
            value=raw,
            message=f"type {raw!r} is not declared in the type registry",
        )

    parsed_kwargs: dict[str, int] = {}
    for slot, value in zip(entry.parameters, raw_args):
        # Strip internal whitespace so French-style grouped numbers like
        # `VARCHAR(40 000 000)` parse the same as `VARCHAR(40000000)`. Handles
        # regular and non-breaking whitespace via `\s+` (Unicode by default).
        compact = re.sub(r"\s+", "", value)
        try:
            parsed_kwargs[slot] = int(compact)
        except ValueError:
            return None, RejectionError(
                kind="unknown_type",
                sheet_row=sheet_row,
                value=raw,
                message=f"parameter {slot!r} for type {raw!r} must be an integer",
            )

    return ParsedType(type=entry.canonical, **parsed_kwargs), None


def unknown_parsed_type() -> ParsedType:
    """Used when --allow-unknown-types downgrades the rejection to a warning."""
    return ParsedType(type=Type.UNKNOWN)
