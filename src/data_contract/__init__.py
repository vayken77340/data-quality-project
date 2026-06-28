"""data_contract -- spec to contract + file-side validation.

Layout (verb-axis):

    data_contract/
    |-- generation/          spec -> contract pipeline
    |-- validation/          file-driven contract + data -> quality report
    |-- data_parsers/        file-format parser registry (dev extension point)
    |-- cli.py               CLI dispatcher
    `-- __main__.py          entry point

Shared primitives (contract dataclasses, type registry, settings,
constraint/check/metric plugin registries, report writers) now live in
`dq_core/`. The warehouse-side validator (Phase 2) will be a peer
package `warehouse_validation/` that imports the same `dq_core/`
primitives.

The one extension registry that stays in `data_contract/` is the
file-format parser registry (`data_parsers/`); warehouse pipelines use
connectors, not file parsers, so the registry stays here.
"""

from __future__ import annotations

__version__ = "0.1.0"

from data_contract.data_parsers import (
    FileParser,
    ParsedFile,
    ParserSchema,
    register as register_parser,
)

__all__ = [
    "__version__",
    "FileParser", "ParsedFile", "ParserSchema", "register_parser",
]
