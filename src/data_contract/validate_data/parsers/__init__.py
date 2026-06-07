"""Parser registry.

Adding a new file format:
1. Drop a module under `validate_data/parsers/` with a `FileParser` subclass.
2. Set `name`, `extensions`, `PARSER_PARAMS`, `DEFAULTS`, and implement `read()`.
3. Register the module path in `_BUILTIN_MODULES` below.

Registration enforces:
- `name` uniqueness across the registry
- Extensions don't clash with any other registered parser
- Docstring contains the required headers (`Spec params:`, `Reads via:`, `Multi-file:`)
"""

from __future__ import annotations

from importlib import import_module

from data_contract.errors import ConfigError
from data_contract.validate_data.parsers.base import FileParser


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


_BUILTIN_MODULES = (
    "data_contract.validate_data.parsers.csv",
    "data_contract.validate_data.parsers.excel",
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
    "register",
    "get_by_name",
    "get_by_extension",
]
