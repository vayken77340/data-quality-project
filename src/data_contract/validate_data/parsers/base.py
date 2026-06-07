"""File parser base class.

Mirrors the `FieldConstraint` pattern: each parser declares `name`,
`extensions`, an allowlist of YAML config keys, and class-level defaults.
Polars is lazy-imported by subclasses so importing this module doesn't
require the validate-data extras.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from data_contract.errors import ConfigError


class FileParser(ABC):
    """ABC for pluggable file-format parsers.

    Spec params:    declared via PARSER_PARAMS (allowlist of YAML config keys).
    Reads via:      subclass-specific Polars reader (lazy-imported).
    Multi-file:     read() takes a list of paths, returns one unified LazyFrame
                    with `__source_file__` and `__row_index__` columns added.
    """

    name: ClassVar[str] = ""
    extensions: ClassVar[tuple[str, ...]] = ()
    PARSER_PARAMS: ClassVar[tuple[str, ...]] = ()
    DEFAULTS: ClassVar[dict[str, Any]] = {}

    params: dict[str, Any]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        raw = dict(params or {})
        unknown = sorted(set(raw) - set(self.PARSER_PARAMS))
        if unknown:
            raise ConfigError(
                f"parser {self.name!r}: unknown config keys {unknown}; "
                f"accepted: {list(self.PARSER_PARAMS)}"
            )
        self.params = {**self.DEFAULTS, **raw}

    @abstractmethod
    def read(self, paths: list[Path], *, table_name_hint: str | None = None) -> Any:
        """Return a Polars LazyFrame uniting all `paths`.

        Implementations MUST add two columns at load time:
          - `__source_file__`: the file each row came from (just the filename).
          - `__row_index__`:   1-based row number within that source file.

        `table_name_hint` is a runtime context supplied by the runner (the
        contract's table key). Parsers that have a notion of "section within
        the file" (e.g. Excel sheets) may use it as a fallback target when no
        explicit selector is configured. Parsers without that notion ignore it.
        """
