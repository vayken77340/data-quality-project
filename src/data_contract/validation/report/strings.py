"""Loader for externalized report UI strings.

All user-facing strings emitted by the XLSX and Markdown writers live in
`configs/report_strings.yaml`. This module loads that file once per process
and exposes a small typed accessor. Missing keys raise KeyError loudly --
a typo in the YAML or a code reference to a removed key should fail fast,
never silently fall back to a hardcoded default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from data_contract.core.yaml_io import load_yaml


_REPO_ROOT = Path(__file__).resolve().parents[4]
_STRINGS_PATH = _REPO_ROOT / "configs" / "report_strings.yaml"


class ReportStrings:
    """Dotted accessor over the report-strings YAML tree."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def get(self, *path: str) -> Any:
        """Walk the YAML tree by key path. Raises KeyError on miss."""
        node: Any = self._d
        for key in path:
            try:
                node = node[key]
            except (KeyError, TypeError):
                raise KeyError(
                    f"report_strings: missing key path {'.'.join(path)!r}"
                ) from None
        return node

    def fmt(self, *path: str, **kwargs: Any) -> str:
        """Get a template at `path` and substitute `{placeholders}`."""
        template = self.get(*path)
        if not isinstance(template, str):
            raise TypeError(
                f"report_strings: key path {'.'.join(path)!r} is not a string template"
            )
        return template.format(**kwargs)


@lru_cache(maxsize=1)
def load_strings() -> ReportStrings:
    """Return the process-wide singleton, loading the YAML on first call."""
    return ReportStrings(load_yaml(_STRINGS_PATH))
