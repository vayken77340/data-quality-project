"""Shared fixtures for the warehouse_validation test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterable

import pytest

from warehouse_validation.connectors.base import Connector


class FakeConnector(Connector):
    """In-memory Connector for runner tests. Matches incoming SQL against
    `canned_counts` keys via substring lookup (so tests don't have to
    pin the exact SQL string the runner emits). Any unmatched query
    returns 0. `execute_columns` returns the configured set for the
    requested table name, or an empty set when no entry was registered.
    """

    def __init__(
        self,
        canned_counts: dict[str, int] | None = None,
        columns_by_table: dict[str, set[str]] | None = None,
    ) -> None:
        self.canned_counts: dict[str, int] = dict(canned_counts or {})
        self.columns_by_table: dict[str, set[str]] = {
            k: set(v) for k, v in (columns_by_table or {}).items()
        }
        self.executed: list[str] = []
        self.column_lookups: list[str] = []

    def execute_scalar(self, sql: str):
        self.executed.append(sql)
        for needle, count in self.canned_counts.items():
            if needle in sql:
                return count
        return 0

    def execute_count(self, sql: str) -> int:
        return int(self.execute_scalar(sql) or 0)

    def execute_columns(self, fq_table: str) -> set[str]:
        self.column_lookups.append(fq_table)
        return set(self.columns_by_table.get(fq_table, set()))


@pytest.fixture
def fake_connector_factory(monkeypatch):
    """Patch warehouse_validation.connectors.get_connector so prepare_run
    returns a FakeConnector instead of trying to open Trino. Returns a
    factory: pass `canned_counts` (and optionally `columns_by_table`) to
    install a FakeConnector and get the instance back so the test can
    inspect `.executed` / `.column_lookups`.
    """
    installed: list[FakeConnector] = []

    def factory(
        canned_counts: dict[str, int] | None = None,
        columns_by_table: dict[str, set[str]] | None = None,
    ) -> FakeConnector:
        conn = FakeConnector(canned_counts, columns_by_table)
        installed.append(conn)
        # Patch every import path the runner / setup chain may reach.
        monkeypatch.setattr(
            "warehouse_validation.setup.get_connector",
            lambda name: conn,
        )
        return conn

    return factory


def write_contract_yaml(
    *,
    epic_root: Path,
    epic: str,
    table: str,
    field_blocks: Iterable[str],
    target: str = "postgres",
    version: str = "1.0",
) -> Path:
    """Write a minimal contract YAML under `epic_root/<epic>/contracts/<table>.yaml`.
    Returns the contract path.
    """
    contracts_dir = epic_root / epic / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    fields_block = "\n".join(field_blocks)
    body = textwrap.dedent(f"""\
        version: "{version}"
        epic: "{epic}"
        generated_at: "2026-06-28T00:00:00Z"
        spec:
          file_path: "tests/synthetic.xlsx"
          sheet_name: "synthetic"
        table: {table}
        target: {target}
        fields:
        """) + fields_block + "\n"
    path = contracts_dir / f"{table}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def warehouse_epic(tmp_path):
    """Build a tmp epic root with a synthetic 1118 contract that carries
    one min_value, one max_value, and one allowed_values constraint --
    one of each pushdown the Phase 2 runner exercises.
    """
    epic_root = tmp_path / "epics"
    write_contract_yaml(
        epic_root=epic_root,
        epic="1118",
        table="synth",
        field_blocks=[
            (
                '  - name: pk\n'
                '    type: int64\n'
                '    nullable: false\n'
                '    primary_key: true\n'
            ),
            (
                '  - name: amount\n'
                '    type: int64\n'
                '    nullable: true\n'
                '    min_value:\n'
                '      value: 0\n'
                '      strict: false\n'
            ),
            (
                '  - name: quota\n'
                '    type: int64\n'
                '    nullable: true\n'
                '    max_value:\n'
                '      value: 100\n'
                '      strict: false\n'
            ),
            (
                '  - name: status\n'
                '    type: string\n'
                '    nullable: true\n'
                '    allowed_values:\n'
                '      - OPEN\n'
                '      - CLOSED\n'
            ),
        ],
    )
    return epic_root
