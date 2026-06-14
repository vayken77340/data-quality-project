"""JSON parser for self-describing exports that carry their own column map.

The expected shape (overridable via `header_path` / `rows_path`):

    {
      "data": {
        "report_header": {
          "c1": {"name": "column 1", "type": "java.lang.String"},
          "c2": {"name": "column 2", "type": "..."}
        },
        "report_row": [
          {"c1": "value 1", "c2": "value 2"},
          ...
        ]
      }
    }

`report_header` is a dict of column-code -> metadata. The parser pulls
the contract-facing name from `metadata[name_key]` (default `"name"`)
and renames every row's keys before yielding them. The `"type"` field
in the header IS surfaced via `ParserSchema.column_types` so the
optional `field_types_from_sample` drift check can compare each declared
source type against the contract.

Why this lives in `json_parser.py` rather than `json.py`: the latter
shadows the stdlib `json` module, breaking `from data_contract.data_parsers
import json` if anyone ever needs it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from data_contract.data_parsers.base import FileParser, ParsedFile, ParserSchema
from data_contract.errors import ConfigError


class JsonParser(FileParser):
    """Read one self-describing JSON export into rows + a ParserSchema.

    Spec params:    encoding, header_path, rows_path, name_key, null_tokens.
    Reads via:      stdlib `json.loads`. Returns an iterable of stringified
                    row dicts via `ParsedFile.rows`; the framework wraps
                    them into a `pl.String` LazyFrame and attaches the
                    tracking columns.
    Multi-file:     handled by `FileParser.read()`.

    Schema:         column_names from `report_header[...][name_key]`.
                    column_types from `report_header[...]["type"]` when
                    present. Java-style native types map to contract
                    `Type` values via `SOURCE_TYPE_ALIASES` below.
    """

    name = "json"
    extensions = (".json",)
    PARSER_PARAMS = (
        "encoding",
        "header_path",
        "rows_path",
        "name_key",
        "null_tokens",
    )
    # No DEFAULTS classvar -- per-format defaults live in `configs/parsers.yaml`.

    # Java-style type strings -> contract Type values (canonical strings in
    # configs/types.yaml). Extend or override per data source.
    SOURCE_TYPE_ALIASES = {
        "java.lang.String":     "string",
        "java.lang.Boolean":    "boolean",
        "java.lang.Integer":    "int32",
        "java.lang.Long":       "int64",
        "java.lang.Short":      "int32",
        "java.lang.Byte":       "int32",
        "java.lang.Float":      "float32",
        "java.lang.Double":     "float64",
        "java.math.BigDecimal": "decimal",
        "java.util.Date":       "date",
        "java.sql.Date":        "date",
        "java.sql.Timestamp":   "timestamp",
    }

    def parse_file(
        self, path: Path, *, table_name_hint: str | None = None
    ) -> ParsedFile:
        import json as _json

        encoding = str(self.params.get("encoding", "utf-8"))
        header_path = self.params.get("header_path")
        rows_path = self.params.get("rows_path")
        name_key = str(self.params.get("name_key", "name"))
        null_tokens = list(self.params.get("null_tokens") or [""])

        if not header_path or not rows_path:
            raise ConfigError(
                f"parser 'json': both `header_path` and `rows_path` are required; "
                f"got header_path={header_path!r}, rows_path={rows_path!r}. "
                f"Set them in configs/parsers.yaml under `json:` or override "
                f"per-table in validation.yaml's parser_overrides."
            )

        payload = _json.loads(path.read_text(encoding=encoding))

        header_dict = _walk_path(payload, str(header_path), source=path)
        if not isinstance(header_dict, dict):
            raise ConfigError(
                f"parser 'json': header_path {header_path!r} in {path.name} "
                f"resolved to {type(header_dict).__name__}, expected a dict of "
                f"column-code -> metadata"
            )

        # code -> contract-field-name. Missing name_key falls back to the
        # code itself; the validation layer will flag unrenamed columns as
        # `extra_column` if they aren't contract fields.
        code_to_name: dict[str, str] = {}
        column_types: dict[str, str] = {}
        for code, meta in header_dict.items():
            if isinstance(meta, dict) and name_key in meta:
                code_to_name[code] = str(meta[name_key])
            else:
                code_to_name[code] = str(code)
            if isinstance(meta, dict) and "type" in meta:
                column_types[code_to_name[code]] = str(meta["type"])

        # Declared column order taken from the header dict's iteration order.
        column_names = list(code_to_name.values())
        # De-dup while preserving order (defensive: two codes mapping to the
        # same name).
        seen: set[str] = set()
        column_names = [n for n in column_names if not (n in seen or seen.add(n))]

        rows_raw = _walk_path(payload, str(rows_path), source=path, allow_missing=True)
        if rows_raw is None:
            rows_raw = []
        if not isinstance(rows_raw, list):
            raise ConfigError(
                f"parser 'json': rows_path {rows_path!r} in {path.name} "
                f"resolved to {type(rows_raw).__name__}, expected a list of row dicts"
            )

        rows: list[dict[str, str | None]] = []
        for row in rows_raw:
            if not isinstance(row, dict):
                raise ConfigError(
                    f"parser 'json': every entry under {rows_path!r} in "
                    f"{path.name} must be an object; got {type(row).__name__}"
                )
            out_row: dict[str, str | None] = {}
            for code, value in row.items():
                col = code_to_name.get(code, str(code))
                out_row[col] = _stringify(value, null_tokens)
            rows.append(out_row)

        return ParsedFile(
            rows=rows,
            schema=ParserSchema(
                column_names=column_names,
                column_types=column_types or None,
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Path segments are either dict keys (no `.` or `[]`) or list indexers
# like `[3]`. Used by `_walk_path` to tokenise dotted paths.
_SEGMENT_RE = re.compile(r"[^.\[\]]+|\[\d+\]")


def _walk_path(payload: Any, dotted_path: str, *, source: Path, allow_missing: bool = False) -> Any:
    """Walk `payload` along `dotted_path`.

    Path syntax:
      * dots separate dict keys: ``a.b.c``
      * ``[N]`` indexes into a list: ``data[0].report_header``,
        ``data[2].rows[1]``

    Raises `ConfigError` with the source file name on missing segments
    unless `allow_missing` is True (in which case missing segments yield
    `None`).
    """
    current: Any = payload
    for segment in _SEGMENT_RE.findall(dotted_path):
        # List indexer like "[0]" -- unwrap and validate.
        if segment.startswith("["):
            idx = int(segment[1:-1])
            if not isinstance(current, list):
                raise ConfigError(
                    f"parser 'json': cannot resolve {dotted_path!r} in {source.name}; "
                    f"segment {segment!r} expected a list, got {type(current).__name__}"
                )
            if idx < 0 or idx >= len(current):
                if allow_missing:
                    return None
                raise ConfigError(
                    f"parser 'json': path {dotted_path!r} not found in {source.name}; "
                    f"index {segment} out of range (list has {len(current)} item(s))"
                )
            current = current[idx]
            continue
        # Dict key.
        if not isinstance(current, dict):
            raise ConfigError(
                f"parser 'json': cannot resolve {dotted_path!r} in {source.name}; "
                f"intermediate segment {segment!r} expected a dict, got "
                f"{type(current).__name__}"
            )
        if segment not in current:
            if allow_missing:
                return None
            raise ConfigError(
                f"parser 'json': path {dotted_path!r} not found in {source.name}; "
                f"segment {segment!r} missing. Top-level keys at the offending "
                f"depth: {sorted(current.keys())}"
            )
        current = current[segment]
    return current


def _stringify(value: Any, null_tokens: list[str]) -> str | None:
    """Render a JSON-loaded value as the canonical string the contract
    layer expects, with `null_tokens` mapped to None.

    Rules:
      - JSON null            -> None.
      - JSON bool/int/float  -> `str(value)` ("true"/"false" for bool).
      - JSON string          -> the string itself, then null-token check.
      - JSON array/object    -> `json.dumps` compact form so structure is
                                preserved but the pl.String invariant
                                holds. The contract layer can flag these
                                as type violations if needed.
    """
    import json as _json

    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return None if value in null_tokens else value
    return _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
