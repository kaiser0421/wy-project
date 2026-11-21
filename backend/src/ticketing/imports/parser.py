"""Bounded row batches with physical line numbers for CSV and JSON."""

import csv
import json
import re
from collections.abc import Generator, Iterator
from mmap import ACCESS_READ, mmap
from pathlib import Path

from ticketing.imports.models import Format

FIELDS = ("zone_code", "row_label", "seat_number")
type Row = tuple[int, int | None, str | None, str | None, str | None, str | None]
_WS = re.compile(rb"[ \t\r\n]*")
_NEWLINE = re.compile(rb"\r\n|\r|\n")
_TOKEN = re.compile(rb'"(?:[^"\\]++|\\[\s\S])*+"|[\[\]{},"]|[^\[\]{},"]+')


class InvalidLayout(Exception):
    def __init__(self, reason: str, line: int = 1, index: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.line = line
        self.index = index


class _Object(dict[str, object]):
    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__(pairs)
        self.duplicate = len(pairs) != len(self)


class _NonJson(Exception):
    pass


def _reject_constant(value: str) -> object:
    raise _NonJson


_DECODER = json.JSONDecoder(object_pairs_hook=_Object, parse_constant=_reject_constant)


def _row(line: int, index: int | None, values: list[object], error: str | None = None) -> Row:
    if len(values) != 3:
        return line, index, None, None, None, error or "invalid_fields"
    clean: list[str | None] = []
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or "\x00" in value:
            clean.append(None)
            error = error or "invalid_fields"
        else:
            # Preserve representative error text without amplifying the error file or staging.
            clean.append(value[:256].encode("utf-8", errors="replace").decode())
            if len(value) > 256:
                error = error or "invalid_fields"
    return line, index, clean[0], clean[1], clean[2], error


def _csv_rows(path: Path) -> Iterator[Row]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        line = 1
        try:
            if next(reader, None) != list(FIELDS):
                raise InvalidLayout("invalid_header")
            while True:
                line = reader.line_num + 1
                values = next(reader, None)
                if values is None:
                    return
                if any("\x00" in value for value in values):
                    raise InvalidLayout("invalid_csv", line)
                yield _row(line, None, list(values))
        except UnicodeError as exc:
            raise InvalidLayout("invalid_encoding", line) from exc
        except csv.Error as exc:
            raise InvalidLayout("invalid_csv", line) from exc


# A seat item is ~100 bytes; a file containing an item over 64 KiB (even pretty-printed, far
# beyond any real seat) is rejected without decoding it.
MAX_JSON_ITEM_BYTES = 64 * 1024


def _item_end(data: mmap, start: int) -> tuple[int, int]:
    """Frame one JSON value in byte offsets, leaving value decoding to raw_decode."""
    depth = peak = 0
    for token in _TOKEN.finditer(data, start):
        offset = token.start()
        char = data[offset]
        if depth == 0 and char in (ord(","), ord("]")):
            return offset, peak
        if char in (ord("["), ord("{")):
            depth += 1
            peak = max(peak, depth)
        elif char in (ord("]"), ord("}")):
            depth -= 1
        elif char == ord('"') and token.end() == offset + 1:
            break
    return len(data), peak


def _whitespace(data: mmap, offset: int) -> int:
    match = _WS.match(data, offset)
    assert match is not None
    return match.end()


def _json_rows(data: mmap) -> Iterator[Row]:
    offset = _whitespace(data, 3 if data[:3] == b"\xef\xbb\xbf" else 0)
    if data[offset : offset + 1] != b"[":
        raise InvalidLayout("invalid_json")
    start = _whitespace(data, offset + 1)
    line = 1 + sum(1 for _ in _NEWLINE.finditer(data, 0, start))
    index = 0
    if data[start : start + 1] == b"]" and _whitespace(data, start + 1) == len(data):
        return
    while start < len(data):
        end, depth = _item_end(data, start)
        if end == start or end == len(data):
            raise InvalidLayout("invalid_json", line, index)
        if len(data[start:end].rstrip(b" \t\r\n")) > MAX_JSON_ITEM_BYTES:
            # Never decode an oversized item: a single huge value inside the 20 MiB upload limit
            # could expand to gigabytes of Python objects. No seat item is this large, so the
            # file is rejected as a whole (never partially applied with skip_invalid).
            raise InvalidLayout("invalid_json", line, index)
        yield from _decode_item(data, start, end, depth, line, index)
        if data[end] == ord("]"):
            if _whitespace(data, end + 1) != len(data):
                raise InvalidLayout("invalid_json", line, index)
            return
        following = _whitespace(data, end + 1)
        line += sum(1 for _ in _NEWLINE.finditer(data, start, following))
        start = following
        index += 1
    raise InvalidLayout("invalid_json", line, index)


def _decode_item(
    data: mmap, start: int, end: int, depth: int, line: int, index: int
) -> Iterator[Row]:
    try:
        raw = data[start:end].decode("utf-8")
        value, consumed = _DECODER.raw_decode(raw)
        if raw[consumed:].strip(" \t\r\n"):
            raise InvalidLayout("invalid_json", line, index)
    except (UnicodeError, ValueError, _NonJson) as exc:
        raise InvalidLayout("invalid_json", line, index) from exc
    except RecursionError as exc:
        if depth <= 16:
            raise InvalidLayout("invalid_json", line, index) from exc
        value = None
    error = "nesting_too_deep" if depth > 16 else None
    if isinstance(value, _Object):
        if value.duplicate:
            error = error or "duplicate_fields"
        if set(value) == set(FIELDS):
            yield _row(line, index, [value[key] for key in FIELDS], error)
        else:
            yield _row(line, index, [], error)
    else:
        yield _row(line, index, [], error)


def iter_rows(path: Path, format_: Format) -> Generator[Row, None, None]:
    if format_ == "csv":
        yield from _csv_rows(path)
        return
    with path.open("rb") as source:
        if source.peek(1) == b"":
            raise InvalidLayout("invalid_json")
        with mmap(source.fileno(), 0, access=ACCESS_READ) as mapped:
            yield from _json_rows(mapped)
