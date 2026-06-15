"""Parser registry.

Adding a new file format:

1. Drop a module under `data_parsers/` with a `FileParser` subclass.
2. Set `name`, `extensions`, `PARSER_PARAMS`, and implement `parse_file()`.
3. Register the module path in `_BUILTIN_MODULES` below.
4. Add a default block for the new parser under `configs/parsers.yaml`
   (or omit it).

Registration enforces:

- `name` uniqueness across the registry
- Extensions don't clash with any other registered parser
- Docstring contains the required headers (`Spec params:`, `Reads via:`,
  `Multi-file:`)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data_contract.core.registry import BaseRegistry, IndexSpec, RegistrySpec
from data_contract.core.yaml_io import load_yaml_mapping
from data_contract.data_parsers.base import (
    FileParser,
    ParsedFile,
    ParserSchema,
    ReadResult,
)
from data_contract.errors import ConfigError


_BUILTIN_MODULES = (
    "data_contract.data_parsers.csv",
    "data_contract.data_parsers.excel",
    "data_contract.data_parsers.json_parser",
)


def _validate_extension(key: str, owner_qualname: str) -> None:
    if not key.startswith("."):
        raise ConfigError(
            f"FileParser {owner_qualname}: extension {key!r} must start with a dot"
        )


_R: BaseRegistry[FileParser] = BaseRegistry(RegistrySpec(
    base_class=FileParser,
    builtin_modules=_BUILTIN_MODULES,
    required_class_attrs=("name", "extensions"),
    required_doc_headers=("Spec params:", "Reads via:", "Multi-file:"),
    secondary_indexes=(
        IndexSpec(
            name="extensions", attr="extensions", unit="iterable",
            key_validator=_validate_extension,
        ),
    ),
))
_R.load_builtins()

REGISTRY = _R.REGISTRY
register = _R.register


def get_by_name(name: str) -> type[FileParser]:
    return _R.get(name)


def get_by_extension(suffix: str) -> type[FileParser] | None:
    """Lookup by filename suffix (e.g. `.csv`). Returns None for unknown extensions."""
    return _R.get_by("extensions", suffix.lower())


def load_parser_yaml_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """Parse the global per-format defaults YAML.

    Returns `{format_name: {key: value, ...}}`. Missing file is OK -- the
    function returns `{}`.

    Load-time validation:
      - Top-level YAML must be a mapping.
      - Each top-level key must match a registered parser `name`.
      - Each block must be a mapping whose keys are a subset of that
        parser's `PARSER_PARAMS` allowlist (typo gate).
    """
    if not path.is_file():
        return {}
    raw = load_yaml_mapping(path, what="parser overrides")
    out: dict[str, dict[str, Any]] = {}
    for fmt, block in raw.items():
        if fmt not in REGISTRY:
            raise ConfigError(
                f"{path}: unknown parser format {fmt!r}; "
                f"registered: {sorted(REGISTRY)}"
            )
        if block is None:
            out[fmt] = {}
            continue
        if not isinstance(block, dict):
            raise ConfigError(
                f"{path}: '{fmt}' block must be a mapping (or omitted); "
                f"got {type(block).__name__}"
            )
        # Include the base-class generic params (e.g. field_matching_policy)
        # so the typo-gate matches what the FileParser `__init__` would accept.
        allowed = (
            set(REGISTRY[fmt].PARSER_PARAMS)
            | set(REGISTRY[fmt]._BASE_PARSER_PARAMS)
        )
        unknown = sorted(set(block) - allowed)
        if unknown:
            raise ConfigError(
                f"{path}: '{fmt}' has unknown keys {unknown}; "
                f"accepted: {sorted(allowed)}"
            )
        out[fmt] = dict(block)
    return out


__all__ = [
    "REGISTRY",
    "FileParser",
    "ParsedFile",
    "ParserSchema",
    "ReadResult",
    "register",
    "get_by_name",
    "get_by_extension",
    "load_parser_yaml_overrides",
]
