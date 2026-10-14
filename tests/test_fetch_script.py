import gzip
import io
import json

from scripts.fetch_px4_logs import _entries


class _ChunkedBytesIO(io.BytesIO):
    def read(self, size=-1):
        if size < 0:
            raise AssertionError("parser tried to buffer the whole compressed index")
        return super().read(min(size, 7))


def test_entries_streams_gzipped_index():
    expected = [
        {"log_id": "first", "duration_s": 12},
        {"log_id": "second", "duration_s": 34},
    ]
    compressed = gzip.compress(json.dumps(expected).encode())

    assert list(_entries(_ChunkedBytesIO(compressed))) == expected
