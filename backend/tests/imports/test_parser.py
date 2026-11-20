import json

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from ticketing.imports.parser import InvalidLayout, iter_rows


@given(st.lists(st.tuples(st.integers(0, 4), st.text(max_size=20)), min_size=1, max_size=20))
@hypothesis_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_json_line_numbers_match_physical_source(tmp_path, rows):
    path = tmp_path / "seats.json"
    data = "\ufeff[\r\n"
    expected = []
    for index, (blank, name) in enumerate(rows):
        data += "\r\n" * blank
        expected.append((data.count("\n") + 1, index))
        row = {"zone_code": "A", "row_label": name, "seat_number": index + 1}
        data += json.dumps(row, indent=index % 3, ensure_ascii=False)
        data += ",\r\n" if index + 1 < len(rows) else "\r\n]"
    path.write_bytes(data.encode())
    assert [row[:2] for row in iter_rows(path, "json")] == expected


def test_csv_multiline_record_reports_physical_line(tmp_path):
    path = tmp_path / "seats.csv"
    path.write_bytes(b'zone_code,row_label,seat_number\r\nA,"one\r\ntwo",1\r\nA,three,2\r\n')
    assert [row[0] for row in iter_rows(path, "csv")] == [2, 4]


def test_json_duplicate_fields_and_invalid_structure(tmp_path):
    path = tmp_path / "seats.json"
    path.write_text('[{"zone_code":"A","zone_code":"B","row_label":"R","seat_number":"1"}]')
    assert next(iter_rows(path, "json"))[-1] == "duplicate_fields"
    path.write_text('[{"zone_code":"A"},]')
    with pytest.raises(InvalidLayout, match="invalid_json"):
        list(iter_rows(path, "json"))
