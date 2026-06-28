"""Universal canonical type system + per-target overlays.

Owns three things the rest of the codebase consumes:
  * `Type` -- the canonical enum (`STRING`, `BIGINT`, `BOOLEAN`, ...) carried
    in every generated contract YAML. Source-side aliases (Oracle's
    `VARCHAR`, French `Nombre entier`, etc.) resolve into one of these.
  * `TypeRegistry` -- the merged view of `configs/types.yaml` + the active
    `configs/targets/<name>.yaml` overlay. Resolves a raw spec type string
    to a `Type`, computes the physical type, and exposes per-type numeric
    bounds, length unit, and parse formats to the validator.
  * `load_type_registry` -- the YAML reader the build pipeline and the
    validator both use to construct a registry.

This module is framework-internal-import-free (only `core.yaml_io` and
`errors`), so contract.py can pull `Type` in without dragging generation
code along.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dq_core.yaml_io import load_yaml
from dq_core.errors import ConfigError, RejectionError


class Type(str, Enum):
    """Universal canonical types carried in generated contract YAMLs.

    Arrow / Iceberg flavored. Source-side aliases (Oracle's `VARCHAR`,
    `INTEGER`, `DOUBLE`, `FLOAT`, French `Booléen` / `Date horodatée`, etc.)
    resolve to these via configs/types.yaml's alias lists.

    Legacy Oracle-flavored canonicals (`varchar`, `integer`, `double`, `float`)
    that appear in old contract YAMLs are resolved by `from_canonical_string`
    below; they never appear as members of this enum.
    """
    STRING       = "string"
    TEXT         = "text"             # variable-length / unbounded string (TEXT, CLOB, etc.)
    INT32        = "int32"
    INT64        = "int64"
    FLOAT32      = "float32"
    FLOAT64      = "float64"
    DECIMAL      = "decimal"
    BOOLEAN      = "boolean"
    DATE         = "date"
    TIMESTAMP    = "timestamp"        # naive (no tz)
    TIMESTAMP_TZ = "timestamp_tz"
    BINARY       = "binary"
    UNKNOWN      = "unknown"

    @classmethod
    def from_canonical_string(cls, s: str) -> "Type":
        """Resolve a contract-YAML `type:` string to a Type member.

        Accepts every current `Type.value` plus the legacy Oracle-flavored
        canonicals (varchar -> STRING, integer -> INT64, double -> FLOAT64,
        float -> FLOAT32). The `integer -> INT64` choice is deliberate:
        Oracle's INTEGER is NUMBER(38), so widening is safe; narrowing would
        false-flag valid IDs.

        Raises ValueError on unknown strings.
        """
        s_norm = str(s).strip().lower()
        legacy = _LEGACY_CANONICAL_ALIASES.get(s_norm)
        if legacy is not None:
            return legacy
        return cls(s_norm)


_LEGACY_CANONICAL_ALIASES: dict[str, Type] = {
    "varchar": Type.STRING,
    "integer": Type.INT64,
    "double":  Type.FLOAT64,
    "float":   Type.FLOAT32,
}


@dataclass(frozen=True)
class _MappingEntry:
    canonical: Type
    aliases: tuple[str, ...]
    parameters: tuple[str, ...]  # () | ("max_length",) | ("precision","scale")
    # Optional data-side token lists. Currently only `boolean` uses this:
    # maps each canonical literal ("true" / "false") to the set of source tokens
    # that should be coerced into it during data validation. Tokens are stored
    # lower-cased and whitespace-stripped, matching the validator's comparison form.
    data_values: dict[str, frozenset[str]] | None = None
    # Optional strptime format list for DATE / TIMESTAMP. Order matters --
    # the data validator tries each in turn and takes the first match. Stored
    # case-preserving (lowercasing would corrupt `%Y` -> `%y`).
    parse_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedType:
    type: Type
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None


@dataclass
class TypeRegistry:
    entries: list[_MappingEntry] = field(default_factory=list)
    # Optional target overlay applied on top of the base entries. Set via
    # `with_target`. Type is `TargetConfig | None`; declared as `Any` to avoid
    # an import cycle (targets.py imports Type).
    target: Any = None
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

    # -- overlay-aware accessors --------------------------------------------

    def data_values_for(self, canonical: Type) -> dict[str, frozenset[str]] | None:
        """Return the universal data_values token map for a canonical type.

        Targets no longer override `data_values`; tokens live on each
        contract field, stamped from configs/types.yaml at contract
        generation. This accessor returns the base universal token list
        (used as a legacy fallback by the validator and as the source for
        the contract-time stamping).
        """
        for entry in self.entries:
            if entry.canonical is canonical and entry.data_values is not None:
                return entry.data_values
        return None

    def parse_formats_for(self, canonical: Type) -> tuple[str, ...]:
        """Return the strptime format list for DATE / TIMESTAMP / TIMESTAMP_TZ,
        or () if absent. Target overlay wins.

        Order is preserved from configs/types.yaml; the data validator tries each
        format in turn and takes the first match.
        """
        ov = self._overlay_for(canonical)
        if ov is not None and ov.parse_formats:
            return ov.parse_formats
        for entry in self.entries:
            if entry.canonical is canonical and entry.parse_formats:
                return entry.parse_formats
        return ()

    def bounds_for(self, canonical: Type) -> tuple[Any, Any] | None:
        """Return the numeric / decimal bounds from the target overlay, or None.

        The base `configs/types.yaml` declares no bounds -- bounds are
        target-specific by design (Postgres INT32 has a 32-bit range; Oracle's
        legacy INTEGER is NUMBER(38)).
        """
        ov = self._overlay_for(canonical)
        return ov.bounds if ov is not None else None

    def length_unit_for(self, canonical: Type) -> str:
        """Return 'bytes' or 'characters' for STRING.

        Only the active target supplies this -- there is no universal default.
        Raises ConfigError if no target overrides STRING.length_unit (this is
        a target config bug: every target that supports STRING must declare it).
        """
        if canonical is not Type.STRING:
            return "characters"   # only meaningful for STRING; harmless fallback
        ov = self._overlay_for(canonical)
        if ov is None or ov.length_unit is None:
            raise ConfigError(
                "length_unit is not defined for STRING in the active target. "
                "Every target that supports STRING must declare `length_unit: "
                "bytes` or `length_unit: characters`."
            )
        return ov.length_unit

    def max_precision_for(self, canonical: Type) -> int | None:
        """Return the per-target max precision for DECIMAL, or None."""
        ov = self._overlay_for(canonical)
        return ov.max_precision if ov is not None else None

    def physical_type_for(self, field: Any) -> str | None:
        """Render the target's physical type for `field`, with placeholder substitution.

        Placeholders accepted in the YAML template: `{max_length}`, `{precision}`,
        `{scale}`. Substituted from `field.max_length` / `field.precision` /
        `field.scale`.

        Returns None when:
          - no target is configured (base registry has no physical_type), or
          - the canonical has no overrides entry.

        Raises ConfigError when the template references a placeholder the
        field doesn't provide (e.g. a STRING field with no max_length under a
        target template `VARCHAR2({max_length} BYTE)`) -- that's a contract /
        target mismatch the caller needs to know about.
        """
        ov = self._overlay_for(field.type)
        if ov is None or not ov.physical_type:
            return None
        # Build substitutions from the field. Only attributes that are set get
        # included; missing-placeholder errors propagate as KeyError -> ConfigError.
        subs: dict[str, Any] = {}
        for attr in ("max_length", "precision", "scale"):
            val = getattr(field, attr, None)
            if val is not None:
                subs[attr] = val
        try:
            return ov.physical_type.format(**subs)
        except KeyError as missing:
            raise ConfigError(
                f"physical_type template for {field.type.value!r} "
                f"references {{{missing.args[0]}}} but field {field.name!r} "
                f"does not declare {missing.args[0]}"
            ) from None

    # -- overlay management -------------------------------------------------

    def with_target(self, target: Any) -> "TypeRegistry":
        """Return a NEW TypeRegistry whose accessors return target-overridden
        values where set, base values otherwise.

        Idempotent: `with_target(None)` returns self. The base entries and
        alias index are reused -- only the overlay reference differs.
        """
        if target is None:
            return self
        new = TypeRegistry(entries=self.entries, target=target)
        return new

    def _overlay_for(self, canonical: Type):
        if self.target is None:
            return None
        return self.target.overrides.get(canonical)


_PAREN_RE = re.compile(r"^(?P<base>[^()]+)\s*\(\s*(?P<args>[^()]*)\s*\)\s*$")
_WS = re.compile(r"\s+")


def _normalize_type_string(s: str) -> str:
    return _WS.sub("", str(s).strip().lower())


def load_type_registry(path: Path) -> TypeRegistry:
    """Load the canonical type registry from a YAML file.

    The YAML is keyed by canonical type name (matching `Type.value`):

        mappings:
          string:
            aliases: [string, varchar, ...]
            parameter: max_length
          int64:
            aliases: [int64, integer, ...]

    The legacy list-of-entries shape (with `canonical:` as a field) is
    rejected with a migration hint.
    """
    raw = load_yaml(path)
    mappings = raw.get("mappings")
    if isinstance(mappings, list):
        raise ConfigError(
            f"{path}: 'mappings' is a list; this shape is no longer supported. "
            f"Use a mapping keyed by canonical name "
            f"(e.g. `mappings:\\n  string:\\n    aliases: [...]`)."
        )
    if not isinstance(mappings, dict):
        raise ConfigError(f"{path}: expected top-level 'mappings' mapping")

    entries: list[_MappingEntry] = []
    valid_canonicals = {t.value for t in Type if t is not Type.UNKNOWN}
    for canonical, body in mappings.items():
        if canonical not in valid_canonicals:
            raise ConfigError(
                f"{path}: mappings.{canonical!r} is not a recognized canonical; "
                f"valid canonicals: {sorted(valid_canonicals)}"
            )
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: mappings.{canonical!r} body must be a mapping")
        aliases = body.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            raise ConfigError(
                f"{path}: mappings.{canonical!r}.aliases must be a non-empty list"
            )

        parameter = body.get("parameter")
        parameters = body.get("parameters")
        if parameter is not None and parameters is not None:
            raise ConfigError(
                f"{path}: mappings.{canonical!r} cannot have both 'parameter' and 'parameters'"
            )
        if parameter is not None:
            params = (str(parameter),)
        elif parameters is not None:
            if not isinstance(parameters, list):
                raise ConfigError(f"{path}: mappings.{canonical!r}.parameters must be a list")
            params = tuple(str(p) for p in parameters)
        else:
            params = ()

        data_values_raw = body.get("data_values")
        data_values: dict[str, frozenset[str]] | None
        if data_values_raw is None:
            data_values = None
        else:
            data_values = _parse_data_values(data_values_raw, path=path, canonical=canonical)

        parse_formats_raw = body.get("parse_formats")
        parse_formats: tuple[str, ...] = ()
        if parse_formats_raw is not None:
            parse_formats = _parse_parse_formats(
                parse_formats_raw, canonical=canonical, path=path,
            )

        entries.append(
            _MappingEntry(
                canonical=Type(canonical),
                aliases=tuple(str(a) for a in aliases),
                parameters=params,
                data_values=data_values,
                parse_formats=parse_formats,
            )
        )
    return TypeRegistry(entries=entries)


def _parse_data_values(raw: object, *, path: Path, canonical: str) -> dict[str, frozenset[str]]:
    """Validate and normalize a mappings.<canonical>.data_values block."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: mappings.{canonical}.data_values must be a mapping")
    out: dict[str, frozenset[str]] = {}
    seen_tokens: dict[str, str] = {}
    for literal, tokens in raw.items():
        literal_key = str(literal).strip().lower()
        if not isinstance(tokens, list) or not tokens:
            raise ConfigError(
                f"{path}: mappings.{canonical}.data_values[{literal!r}] must be a non-empty list"
            )
        normalized: set[str] = set()
        for token in tokens:
            if isinstance(token, bool):
                norm = "true" if token else "false"
            elif isinstance(token, (int, float)):
                norm = _normalize_data_token(str(token))
            elif isinstance(token, str):
                norm = _normalize_data_token(token)
            else:
                raise ConfigError(
                    f"{path}: mappings.{canonical}.data_values[{literal!r}] tokens must be strings, "
                    f"numbers, or booleans (got {type(token).__name__})"
                )
            if norm in seen_tokens and seen_tokens[norm] != literal_key:
                raise ConfigError(
                    f"{path}: mappings.{canonical}.data_values: token {norm!r} appears under both "
                    f"{seen_tokens[norm]!r} and {literal_key!r}"
                )
            seen_tokens[norm] = literal_key
            normalized.add(norm)
        out[literal_key] = frozenset(normalized)
    return out


def _normalize_data_token(s: str) -> str:
    """Normalize a data-side token for comparison: strip + lowercase."""
    return str(s).strip().lower()


_PARSE_FORMATS_ALLOWED_CANONICALS = frozenset({"date", "timestamp", "timestamp_tz"})


def _parse_parse_formats(
    raw: object, *, canonical: str, path: Path,
) -> tuple[str, ...]:
    """Validate and normalize a mappings.<canonical>.parse_formats block.

    Case- and order-preserving. Only allowed on DATE / TIMESTAMP / TIMESTAMP_TZ
    entries -- declaring it elsewhere is almost certainly a config mistake and
    earns a clean ConfigError so the typo fails at load.
    """
    if canonical not in _PARSE_FORMATS_ALLOWED_CANONICALS:
        raise ConfigError(
            f"{path}: mappings.{canonical}.parse_formats is only valid on "
            f"DATE / TIMESTAMP / TIMESTAMP_TZ canonicals"
        )
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"{path}: mappings.{canonical}.parse_formats must be a non-empty list of strptime format strings"
        )
    out: list[str] = []
    for fmt in raw:
        if not isinstance(fmt, str) or not fmt:
            raise ConfigError(
                f"{path}: mappings.{canonical}.parse_formats entries must be non-empty strings; got {fmt!r}"
            )
        out.append(fmt)
    return tuple(out)


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

    # Bare bounded-string (varchar / varchar2 / char / texte / string / str
    # without a length) is meaningless: there's no max to enforce and the
    # target's physical_type template `VARCHAR2({max_length} BYTE)` would
    # fail to render. Promote to the variable-length canonical TEXT instead.
    # That way `VARCHAR2(100)` stays a bounded STRING but bare `VARCHAR2`
    # becomes the unbounded variant (CLOB on Oracle, TEXT on Postgres,
    # string on Iceberg).
    if entry.canonical is Type.STRING and not raw_args:
        return ParsedType(type=Type.TEXT), None

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
