"""Parser registry.

Adding a new file format:

1. Drop a module under `data_parsers/` with a `FileParser` subclass.
2. Set `name`, `extensions`, `PARSER_PARAMS`, and implement `read()`.
3. Register the module path in `_BUILTIN_MODULES` below.
4. Add a default block for the new parser under `configs/parsers.yaml`
   (or omit it -- the parser receives an empty params dict in that case,
   plus whatever validation.yaml supplies as overrides).

Registration enforces:

- `name` uniqueness across the registry
- Extensions don't clash with any other registered parser
- Docstring contains the required headers (`Spec params:`, `Reads via:`,
  `Multi-file:`)

See `EXTENDING.md` next to this folder for the full recipe.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import yaml

from data_contract.errors import ConfigError
from data_contract.data_parsers.base import (
    FileParser,
    ParsedFile,
    ParserSchema,
    ReadResult,
)


REGISTRY: dict[str, type[FileParser]] = {}
_EXTENSION_INDEX: dict[str, type[FileParser]] = {}

_REQUIRED_DOCSTRING_HEADERS = ("Spec params:", "Reads via:", "Multi-file:")


def register(cls: type[FileParser]) -> type[FileParser]:
    if not isinstance(cls.name, str) or not cls.name:
        raise ConfigError(f"FileParser {cls.__qualname__} must declare a non-empty `name`")
    if not cls.extensions:
        raise ConfigError(f"FileParser {cls.__qualname__} must declare at least one extension")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ConfigError(
            f"FileParser name {cls.name!r} already registered by {REGISTRY[cls.name].__qualname__}"
        )
    for ext in cls.extensions:
        if not ext.startswith("."):
            raise ConfigError(
                f"FileParser {cls.__qualname__}: extension {ext!r} must start with a dot"
            )
        existing = _EXTENSION_INDEX.get(ext)
        if existing is not None and existing is not cls:
            raise ConfigError(
                f"FileParser {cls.__qualname__}: extension {ext!r} already claimed by "
                f"{existing.__qualname__}"
            )
    doc = (cls.__doc__ or "").strip()
    if not doc:
        raise ConfigError(
            f"FileParser {cls.__qualname__} must have a non-empty docstring; "
            f"document its spec params, reader, and multi-file behavior"
        )
    missing = [h for h in _REQUIRED_DOCSTRING_HEADERS if h not in doc]
    if missing:
        raise ConfigError(
            f"FileParser {cls.__qualname__} docstring must include headers "
            f"{list(_REQUIRED_DOCSTRING_HEADERS)}; missing: {missing}"
        )

    REGISTRY[cls.name] = cls
    for ext in cls.extensions:
        _EXTENSION_INDEX[ext] = cls
    return cls


def get_by_name(name: str) -> type[FileParser]:
    if name not in REGISTRY:
        raise ConfigError(f"unknown parser format {name!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[name]


def get_by_extension(suffix: str) -> type[FileParser] | None:
    """Lookup by filename suffix (e.g. `.csv`). Returns None for unknown extensions."""
    return _EXTENSION_INDEX.get(suffix.lower())


def load_parser_yaml_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """Parse the global per-format defaults YAML.

    Returns `{format_name: {key: value, ...}}`. Missing file is OK -- the
    function returns `{}` and the runner falls through to validation.yaml
    overrides for any per-format values.

    Load-time validation:
      - Top-level YAML must be a mapping.
      - Each top-level key must match a registered parser `name`.
      - Each block must be a mapping whose keys are a subset of that
        parser's `PARSER_PARAMS` allowlist (typo gate).
    """
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: top-level YAML must be a mapping of format-name -> overrides"
        )
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
        allowed = set(REGISTRY[fmt].PARSER_PARAMS)
        unknown = sorted(set(block) - allowed)
        if unknown:
            raise ConfigError(
                f"{path}: '{fmt}' has unknown keys {unknown}; "
                f"accepted: {sorted(allowed)}"
            )
        out[fmt] = dict(block)
    return out


_BUILTIN_MODULES = (
    "data_contract.data_parsers.csv",
    "data_contract.data_parsers.excel",
    "data_contract.data_parsers.json_parser",
)


def _load_builtins() -> None:
    for module_path in _BUILTIN_MODULES:
        mod = import_module(module_path)
        for cls in vars(mod).values():
            if (
                isinstance(cls, type)
                and issubclass(cls, FileParser)
                and cls is not FileParser
            ):
                if cls.name and cls.name not in REGISTRY:
                    register(cls)


_load_builtins()


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
