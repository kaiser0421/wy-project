import hashlib

import pytest
from anyio import to_thread

from ticketing.core.errors import ProblemError
from ticketing.imports.upload import store_upload

_CONTENT_TYPE = "multipart/form-data; boundary=seats"
_HEADER = (
    b'--seats\r\nContent-Disposition: form-data; name="file"; filename="seats.csv"\r\n'
    b"Content-Type: text/csv\r\n\r\n"
)


async def _chunks(data: bytes):
    for offset in range(0, len(data), 3):
        yield data[offset : offset + 3]


async def test_multipart_boundary_can_span_receive_chunks(tmp_path):
    content = b"zone_code,row_label,seat_number\nA,R,1\n"
    result = await store_upload(
        _chunks(_HEADER + content + b"\r\n--seats--\r\n"),
        _CONTENT_TYPE,
        str(tmp_path),
        len(content),
    )
    assert result.digest == hashlib.sha256(content).digest()
    assert result.mode == "all_or_nothing"
    assert await to_thread.run_sync(result.path.read_bytes) == content


async def test_truncated_multipart_never_persists_a_file(tmp_path):
    with pytest.raises(ProblemError) as error:
        await store_upload(
            _chunks(_HEADER + b"content\r\n--seats\r\n"),
            _CONTENT_TYPE,
            str(tmp_path),
            100,
        )
    assert error.value.code == "invalid-multipart"
    assert await to_thread.run_sync(lambda: list(tmp_path.iterdir())) == []
