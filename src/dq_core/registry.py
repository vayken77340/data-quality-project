"""Generic plugin registry.

Collapses four near-identical `__init__.py` files (field_constraints,
table_checks, metrics, data_parsers) onto one base. Each sibling registry
becomes a 20-line declaration of `RegistrySpec` + a couple of re-exports.

What every plugin tier shares:
  * a `name` ClassVar identifying the plugin
  * an entry in a tuple of builtin module paths to import at startup
  * a `register` decorator that walks the loaded module and registers
    every subclass

Where they differ:
  * required ClassVars (`VIOLATION_KIND`, `DIMENSION`, `contract_key`, ...)
  * required docstring section headers
  * secondary indexes (FieldConstraint indexes by `contract_key` too;
    FileParser indexes by file `extensions`)

`RegistrySpec` declares those knobs; the rest is uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Generic, TypeVar

from dq_core.errors import ConfigError


T = TypeVar("T")


@dataclass(frozen=True)
class IndexSpec:
    """Secondary index on a class attribute.

    `attr` is the ClassVar to read; `unit` describes how the value
    decomposes:
      * "scalar"  -> the attribute is one key (e.g. contract_key="default")
      * "iterable"-> the attribute is a tuple of keys (e.g. extensions=(".csv",))
    `name` is used in error messages and by `get_by`.
    `key_validator`, if set, is called on each key for shape checks
    (e.g. file extensions must start with a dot).
    """
    name: str
    attr: str
    unit: str = "scalar"
    key_validator: Any = None    # callable[(key, owner_qualname), None] | None


@dataclass(frozen=True)
class RegistrySpec:
    base_class: type
    builtin_modules: tuple[str, ...]
    # ClassVars every registered subclass must declare with a non-empty value.
    # `name` is always required; subclasses may extend (e.g. VIOLATION_KIND).
    required_class_attrs: tuple[str, ...] = ("name",)
    # Sections that must appear in every subclass docstring. Empty tuple
    # disables the docstring gate.
    required_doc_headers: tuple[str, ...] = ()
    # Optional secondary indexes for reverse-lookup (contract_key, extensions, ...).
    secondary_indexes: tuple[IndexSpec, ...] = ()
    # If set, the registry calls this hook on each subclass after the
    # standard checks have passed. Use it for tier-specific validation
    # rules (e.g. FieldConstraint enforces "if check_data is overridden,
    # VIOLATION_KIND must be non-empty").
    extra_validator: Any = None  # callable[(cls,), None] | None


class BaseRegistry(Generic[T]):
    """Shared plugin registry. Each plugin tier instantiates one of these."""

    spec: RegistrySpec
    REGISTRY: dict[str, type[T]]
    _indexes: dict[str, dict[Any, type[T]]]

    def __init__(self, spec: RegistrySpec) -> None:
        self.spec = spec
        self.REGISTRY = {}
        self._indexes = {idx.name: {} for idx in spec.secondary_indexes}

    # -- registration ------------------------------------------------------

    def register(self, cls: type[T]) -> type[T]:
        """Validate and add `cls` to the registry. Idempotent for the same class.

        Returns `cls` so callers may use this as a decorator.
        """
        for attr in self.spec.required_class_attrs:
            value = getattr(cls, attr, None)
            if not _is_present(value):
                raise ConfigError(
                    f"{self.spec.base_class.__name__} {cls.__qualname__} "
                    f"must declare a non-empty `{attr}`"
                )

        if self.spec.required_doc_headers:
            doc = (cls.__doc__ or "").strip()
            if not doc:
                raise ConfigError(
                    f"{self.spec.base_class.__name__} {cls.__qualname__} "
                    f"must have a non-empty docstring"
                )
            missing = [h for h in self.spec.required_doc_headers if h not in doc]
            if missing:
                raise ConfigError(
                    f"{self.spec.base_class.__name__} {cls.__qualname__} "
                    f"docstring must include the headers "
                    f"{list(self.spec.required_doc_headers)}; missing: {missing}"
                )

        name = getattr(cls, "name")
        existing = self.REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ConfigError(
                f"{self.spec.base_class.__name__} name {name!r} already "
                f"registered by {existing.__qualname__}"
            )

        # Secondary index validation -- run BEFORE inserting into REGISTRY
        # so a clash leaves the registry consistent.
        for idx in self.spec.secondary_indexes:
            keys = _index_keys(cls, idx)
            for key in keys:
                if idx.key_validator is not None:
                    idx.key_validator(key, cls.__qualname__)
                # If another (different) class already claims this key, refuse.
                for other_name, other_cls in self.REGISTRY.items():
                    if other_cls is cls:
                        continue
                    if key in _index_keys(other_cls, idx):
                        raise ConfigError(
                            f"{self.spec.base_class.__name__} {cls.__qualname__}: "
                            f"{idx.name} {key!r} already claimed by "
                            f"{other_cls.__qualname__} (name={other_name!r})"
                        )

        if self.spec.extra_validator is not None:
            self.spec.extra_validator(cls)

        self.REGISTRY[name] = cls
        for idx in self.spec.secondary_indexes:
            for key in _index_keys(cls, idx):
                self._indexes[idx.name][key] = cls
        return cls

    # -- lookups -----------------------------------------------------------

    def get(self, name: str) -> type[T]:
        if name not in self.REGISTRY:
            raise ConfigError(
                f"unknown {self.spec.base_class.__name__} {name!r}; "
                f"registered: {sorted(self.REGISTRY)}"
            )
        return self.REGISTRY[name]

    def get_by(self, index: str, key: Any) -> type[T] | None:
        """Reverse-lookup via a secondary index. Returns None on miss.

        Defensively rechecks the primary REGISTRY so callers that pop
        entries out by name (tests, hot-reload) don't see stale hits.
        """
        if index not in self._indexes:
            raise ConfigError(
                f"{self.spec.base_class.__name__}: no secondary index {index!r}"
            )
        cls = self._indexes[index].get(key)
        if cls is None:
            return None
        if self.REGISTRY.get(cls.name) is not cls:
            self._indexes[index].pop(key, None)
            return None
        return cls

    # -- startup loader ----------------------------------------------------

    def load_builtins(self) -> None:
        """Import every builtin module and auto-register its public subclasses.

        A subclass with an unset `name` is skipped (allows abstract intermediates).
        """
        for module_path in self.spec.builtin_modules:
            mod = import_module(module_path)
            for obj in vars(mod).values():
                if not isinstance(obj, type):
                    continue
                if obj is self.spec.base_class:
                    continue
                if not issubclass(obj, self.spec.base_class):
                    continue
                name = getattr(obj, "name", "")
                if not name or name in self.REGISTRY:
                    continue
                self.register(obj)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_present(value: Any) -> bool:
    """True iff a required ClassVar has a non-empty value."""
    if value is None:
        return False
    if isinstance(value, (str, tuple, list, set, frozenset)):
        return bool(value)
    return True


def _index_keys(cls: type, idx: IndexSpec) -> tuple[Any, ...]:
    """Extract the secondary-index keys from a class.

    Missing / falsy attributes contribute no keys (the class is simply
    absent from this index).
    """
    raw = getattr(cls, idx.attr, None)
    if not raw:
        return ()
    if idx.unit == "scalar":
        return (raw,)
    # iterable
    return tuple(raw)
