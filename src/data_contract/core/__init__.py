"""Cross-cutting primitives shared by generation and validation.

The modules here have no domain knowledge — they're the lowest layer the
two pipelines stand on:

  - registry.py    BaseRegistry: collapses the four sibling plugin registries.
  - yaml_io.py     load/dump YAML + require-mapping helper.
  - column_ref.py  ColumnRef + UNSET sentinel + parse_column_ref + read_cell.
  - gates.py       Gates: tier-keyed name -> enabled lookup, shared by
                   validation.yaml `checks:` and `metrics:` parsing.
  - epic.py        Epic-name validation + epic discovery.

Nothing under data_contract/core/ may import from generation/ or
validation/ — that would invert the dependency direction.
"""

from __future__ import annotations
