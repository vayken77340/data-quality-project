from __future__ import annotations

import pytest

from data_contract.errors import ConfigError
from data_contract.validation import parsers
from data_contract.validation.parsers import (
    REGISTRY,
    FileParser,
    get_by_extension,
    get_by_name,
    register,
)


def test_builtins_auto_register():
    assert "csv" in REGISTRY
    assert "excel" in REGISTRY


def test_get_by_name_returns_class():
    cls = get_by_name("csv")
    assert cls.__name__ == "CsvParser"


def test_get_by_name_unknown_raises():
    with pytest.raises(ConfigError, match="unknown parser"):
        get_by_name("parquet")


def test_get_by_extension_is_case_insensitive():
    assert get_by_extension(".CSV").__name__ == "CsvParser"
    assert get_by_extension(".xlsx").__name__ == "ExcelParser"


def test_get_by_extension_unknown_returns_none():
    assert get_by_extension(".unknown") is None


def test_register_rejects_empty_name():
    class NoName(FileParser):
        """Spec params: none.
        Reads via: nothing.
        Multi-file: nothing.
        """
        name = ""
        extensions = (".x",)

        def read(self, paths):
            return None

    with pytest.raises(ConfigError, match="non-empty `name`"):
        register(NoName)


def test_register_rejects_extension_without_dot():
    class BadExt(FileParser):
        """Spec params: none. Reads via: x. Multi-file: x."""
        name = "badext"
        extensions = ("noleadingdot",)

        def read(self, paths):
            return None

    with pytest.raises(ConfigError, match="must start with a dot"):
        register(BadExt)


def test_register_rejects_extension_collision():
    class ConflictsWithCsv(FileParser):
        """Spec params: none. Reads via: x. Multi-file: x."""
        name = "csv_clone"
        extensions = (".csv",)

        def read(self, paths):
            return None

    try:
        with pytest.raises(ConfigError, match="already claimed"):
            register(ConflictsWithCsv)
    finally:
        REGISTRY.pop("csv_clone", None)


def test_register_rejects_duplicate_name():
    class CsvImpostor(FileParser):
        """Spec params: none. Reads via: x. Multi-file: x."""
        name = "csv"
        extensions = (".csvclone",)

        def read(self, paths):
            return None

    with pytest.raises(ConfigError, match="already registered"):
        register(CsvImpostor)


def test_register_requires_docstring():
    class NoDocs(FileParser):
        name = "nodocs"
        extensions = (".nodocs",)

        def read(self, paths):
            return None

    with pytest.raises(ConfigError, match="non-empty docstring"):
        register(NoDocs)


def test_register_requires_three_doc_headers():
    class PartialDocs(FileParser):
        """Just one line, missing the required headers."""
        name = "partialdocs"
        extensions = (".pd",)

        def read(self, paths):
            return None

    with pytest.raises(ConfigError, match="headers"):
        register(PartialDocs)


def test_register_custom_parser_round_trip():
    class JsonLinesParser(FileParser):
        """Tiny custom parser for the test.

        Spec params: none.
        Reads via: nothing meaningful.
        Multi-file: stub.
        """
        name = "jsonl_test"
        extensions = (".jsonl_test",)

        def read(self, paths):
            return None

    try:
        register(JsonLinesParser)
        assert get_by_name("jsonl_test") is JsonLinesParser
        assert get_by_extension(".jsonl_test") is JsonLinesParser
    finally:
        REGISTRY.pop("jsonl_test", None)
        parsers._EXTENSION_INDEX.pop(".jsonl_test", None)


def test_parser_init_rejects_unknown_param():
    from data_contract.validation.parsers.csv import CsvParser

    with pytest.raises(ConfigError, match="unknown config keys"):
        CsvParser({"bogus_key": "value"})


def test_parser_init_merges_defaults():
    from data_contract.validation.parsers.csv import CsvParser

    parser = CsvParser({"delimiter": ";"})
    assert parser.params["delimiter"] == ";"
    assert parser.params["encoding"] == "utf-8"  # from DEFAULTS
    assert parser.params["header_row"] == 1
