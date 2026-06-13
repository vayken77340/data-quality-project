from data_contract.generation.header_matcher import find_column, normalize


def test_normalize_trims_trailing_question_mark():
    assert normalize("Obligatoire ?") == normalize("Obligatoire")


def test_normalize_collapses_whitespace():
    assert normalize("  Champ   dans   extract  ") == "champ dans extract"
    # case + trailing whitespace + trailing question-mark all stripped
    assert normalize("  TYPE  ?  ") == "type"


def test_find_column_handles_none_cells_and_punctuation():
    headers = [None, "Champ dans extract", "Type", "Obligatoire ?"]
    assert find_column(headers, "Obligatoire") == 3
    assert find_column(headers, "type") == 2
    assert find_column(headers, "missing") is None
