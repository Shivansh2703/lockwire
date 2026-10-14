#!/usr/bin/env python3
"""Filter the PX4 public index and download ULog files with metadata sidecars."""

from __future__ import annotations

import argparse
import codecs
import gzip
import json
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Iterator

INDEX_URL = "https://logs.px4.io/dbinfo"


class _PrefixedStream:
    """Replay a sniffed prefix before continuing to read the source."""

    def __init__(self, prefix: bytes, source: BinaryIO) -> None:
        self._prefix = prefix
        self._source = source

    def read(self, size: int = -1) -> bytes:
        if not self._prefix:
            return self._source.read(size)
        if size < 0:
            prefix, self._prefix = self._prefix, b""
            return prefix + self._source.read()
        prefix, self._prefix = self._prefix[:size], self._prefix[size:]
        return prefix + self._source.read(size - len(prefix))


def _entries(response: BinaryIO) -> Iterator[dict[str, Any]]:
    """Incrementally decode the large top-level JSON array."""
    prefix = response.read(2)
    while len(prefix) < 2:
        chunk = response.read(2 - len(prefix))
        if not chunk:
            break
        prefix += chunk
    source = _PrefixedStream(prefix, response)
    headers = getattr(response, "headers", None)
    content_encoding = headers.get("Content-Encoding", "") if headers is not None else ""
    encodings = {encoding.strip().casefold() for encoding in content_encoding.split(",")}
    if prefix == b"\x1f\x8b" or "gzip" in encodings:
        source = gzip.GzipFile(fileobj=source)

    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    started = False
    eof = False
    while True:
        chunk = source.read(1024 * 1024)
        if chunk:
            buffer += utf8.decode(chunk)
        else:
            buffer += utf8.decode(b"", final=True)
            eof = True
        position = 0
        while True:
            while position < len(buffer) and (
                buffer[position].isspace() or buffer[position] in "[,"
            ):
                if buffer[position] == "[":
                    started = True
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                return
            if position >= len(buffer):
                break
            try:
                value, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                break
            if not started or not isinstance(value, dict):
                raise ValueError("PX4 index is not an array of objects")
            yield value
            position = end
        buffer = buffer[position:]
        if eof:
            if buffer.strip() not in ("", "]"):
                raise ValueError("PX4 index ended inside a JSON value")
            return


def _matches(entry: dict[str, Any], args: argparse.Namespace) -> bool:
    if (
        args.airframe
        and args.airframe.casefold() not in str(entry.get("airframe_name", "")).casefold()
    ):
        return False
    if args.mav_type and entry.get("mav_type") != args.mav_type:
        return False
    if float(entry.get("duration_s") or 0) < args.min_duration:
        return False
    labels = entry.get("error_labels") or []
    return args.errors is None or bool(labels) is args.errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airframe", help="case-insensitive substring of airframe_name")
    parser.add_argument("--mav-type", help="exact mav_type, for example Quadrotor")
    parser.add_argument("--min-duration", type=float, default=0.0, metavar="SECONDS")
    errors = parser.add_mutually_exclusive_group()
    errors.add_argument("--has-error-labels", dest="errors", action="store_true")
    errors.add_argument("--no-error-labels", dest="errors", action="store_false")
    parser.set_defaults(errors=None)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("px4-logs"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    args.output.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(INDEX_URL, headers={"Accept-Encoding": "gzip"})
    downloaded = 0
    with urllib.request.urlopen(request) as response:
        for entry in _entries(response):
            if not _matches(entry, args):
                continue
            log_id = str(entry["log_id"])
            destination = args.output / f"{log_id}.ulg"
            with urllib.request.urlopen(str(entry["download_url"])) as source:
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
            (args.output / f"{log_id}.metadata.json").write_text(
                json.dumps(entry, indent=2, sort_keys=True) + "\n"
            )
            print(destination)
            downloaded += 1
            if downloaded == args.limit:
                break
    if not downloaded:
        print("no PX4 logs matched the filters", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
