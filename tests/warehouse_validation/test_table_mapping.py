"""Tests for the warehouse.yaml -> TableMapping loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dq_core.errors import ConfigError
from warehouse_validation.table_mapping import TableMapping, load_mapping


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_yaml_present_with_entry_returns_mapping(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "cat.sch.ipn_project_bronze"
            silver: "cat.sch.ipn_project_silver"
        """)
    mapping = load_mapping(epic_dir, "ipn_project")
    assert mapping == TableMapping(
        bronze="cat.sch.ipn_project_bronze",
        silver="cat.sch.ipn_project_silver",
    )


def test_yaml_present_entry_missing_falls_back_to_convention(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "cat.sch.ipn_project_bronze"
            silver: "cat.sch.ipn_project_silver"
        """)
    mapping = load_mapping(epic_dir, "ipn_calendar")
    assert mapping == TableMapping(
        bronze="ipn_calendar_bronze",
        silver="ipn_calendar_silver",
    )


def test_yaml_missing_entirely_falls_back_to_convention(tmp_path):
    epic_dir = tmp_path / "1118"
    epic_dir.mkdir()
    mapping = load_mapping(epic_dir, "ipn_project")
    assert mapping == TableMapping(
        bronze="ipn_project_bronze",
        silver="ipn_project_silver",
    )


def test_bronze_equals_silver_raises_config_error(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          collide:
            bronze: "cat.sch.same_table"
            silver: "cat.sch.same_table"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "collide")
    assert "collide" in str(exc.value)
    assert "same physical name" in str(exc.value)


def test_tables_must_be_a_mapping(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          - ipn_project
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    assert "'tables' must be a mapping" in str(exc.value)


def test_entry_must_be_a_mapping(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project: "cat.sch.ipn_project_bronze"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    assert "must be a mapping" in str(exc.value)
    assert "ipn_project" in str(exc.value)


def test_entry_missing_bronze_key_raises(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            silver: "cat.sch.ipn_project_silver"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    msg = str(exc.value)
    assert "missing required" in msg
    assert "bronze" in msg


def test_entry_missing_silver_key_raises(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "cat.sch.ipn_project_bronze"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    msg = str(exc.value)
    assert "missing required" in msg
    assert "silver" in msg


def test_top_level_tables_missing_falls_back(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        # no tables: key at all
        other_key: 1
        """)
    mapping = load_mapping(epic_dir, "ipn_project")
    assert mapping == TableMapping(
        bronze="ipn_project_bronze",
        silver="ipn_project_silver",
    )


def test_placeholder_bronze_raises(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "placeholder_catalog.placeholder_schema.ipn_project_bronze"
            silver: "real_cat.real_sch.ipn_project_silver"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    msg = str(exc.value)
    assert "placeholder" in msg
    assert "bronze" in msg
    assert "ipn_project" in msg


def test_placeholder_silver_raises(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "real_cat.real_sch.ipn_project_bronze"
            silver: "placeholder_catalog.placeholder_schema.ipn_project_silver"
        """)
    with pytest.raises(ConfigError) as exc:
        load_mapping(epic_dir, "ipn_project")
    msg = str(exc.value)
    assert "placeholder" in msg
    assert "silver" in msg
    assert "ipn_project" in msg


def test_realistic_names_pass_placeholder_check(tmp_path):
    epic_dir = tmp_path / "1118"
    _write(epic_dir / "configs" / "warehouse.yaml", """\
        tables:
          ipn_project:
            bronze: "prod.bronze.ipn_project"
            silver: "prod.silver.ipn_project"
        """)
    mapping = load_mapping(epic_dir, "ipn_project")
    assert mapping == TableMapping(
        bronze="prod.bronze.ipn_project",
        silver="prod.silver.ipn_project",
    )
