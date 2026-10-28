"""PDF (best effort, classic cross-reference tables only).

Strategy: same-length overwrites. The /Info dictionary is replaced by an
empty ``<<>>`` padded with spaces, ``/Metadata N G R`` references are blanked
with spaces and the XMP streams themselves are overwritten with spaces. No
byte moves, so every offset in the xref table stays valid and nothing has to
be re-serialised.

Files that use cross-reference streams or object streams (common since
PDF 1.5) can hide the Info dictionary inside compressed data; those, and
encrypted files, are refused with a clear message instead of being
"cleaned" half-way.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .core import FormatError, Item, Options, UnsupportedFile

NAME = "PDF"

WS = b" \t\r\n\f\x00"
DELIM = b"()<>[]{}/%"
OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")
REF_RE = re.compile(rb"(\d+)\s+(\d+)\s+R\b")
TRAILER_RE = re.compile(rb"trailer\s*<<")
META_REF_RE = re.compile(rb"/Metadata\s+(\d+)\s+(\d+)\s+R\b")
META_DICT_RE = re.compile(rb"/Type\s*/Metadata\b|/Subtype\s*/XML\b")
LENGTH_RE = re.compile(rb"/Length\s+(\d+\s+\d+\s+R|\d+)")


def sniff(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def _skip_ws(data: bytes, i: int) -> int:
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c in WS and c:
            i += 1
        elif c == b"%":
            while i < n and data[i:i + 1] not in (b"\r", b"\n"):
                i += 1
        else:
            break
    return i


def _string_end(data: bytes, i: int) -> int:
    depth, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash escape
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise FormatError("unterminated PDF string")


def _object_end(data: bytes, i: int) -> int:
    """Return the index just past one PDF object starting at ``i``."""
    i = _skip_ws(data, i)
    n = len(data)
    if data.startswith(b"<<", i):
        i += 2
        while True:
            i = _skip_ws(data, i)
            if i >= n:
                raise FormatError("unterminated PDF dictionary")
            if data.startswith(b">>", i):
                return i + 2
            i = _object_end(data, i)
    c = data[i:i + 1]
    if c == b"(":
        return _string_end(data, i)
    if c == b"<":
        j = data.find(b">", i)
        if j < 0:
            raise FormatError("unterminated PDF hex string")
        return j + 1
    if c == b"[":
        i += 1
        while True:
            i = _skip_ws(data, i)
            if i >= n:
                raise FormatError("unterminated PDF array")
            if data[i:i + 1] == b"]":
                return i + 1
            i = _object_end(data, i)
    if c == b"/":
        i += 1
    start = i
    while i < n and data[i:i + 1] not in WS and data[i:i + 1] not in DELIM:
        i += 1
    if i == start and c != b"/":
        raise FormatError(f"unexpected PDF token at offset {i}")
    m = re.compile(rb"\s+\d+\s+R\b").match(data, i)
    if m and data[start:i].isdigit():
        return m.end()
    return i


def _dict_keys(data: bytes, i: int) -> Tuple[List[str], int]:
    """Parse a dictionary at ``i``; return its key names and its end offset."""
    i = _skip_ws(data, i)
    if not data.startswith(b"<<", i):
        raise FormatError("expected a PDF dictionary")
    keys, i, n = [], i + 2, len(data)
    while True:
        i = _skip_ws(data, i)
        if i >= n:
            raise FormatError("unterminated PDF dictionary")
        if data.startswith(b">>", i):
            return keys, i + 2
        if data[i:i + 1] != b"/":
            raise FormatError("expected a name as PDF dictionary key")
        k_end = _object_end(data, i)
        keys.append(data[i:k_end].decode("latin-1"))
        i = _object_end(data, k_end)


def _objects(data: bytes) -> Dict[Tuple[int, int], List[int]]:
    """Map (num, gen) -> offsets just after 'obj' for every definition."""
    out: Dict[Tuple[int, int], List[int]] = {}
    for m in OBJ_RE.finditer(data):
        out.setdefault((int(m.group(1)), int(m.group(2))), []).append(m.end())
    return out


def _trailers(data: bytes) -> List[Tuple[int, int]]:
    out = []
    for m in TRAILER_RE.finditer(data):
        start = m.end() - 2
        _, end = _dict_keys(data, start)
        out.append((start, end))
    return out


def _check_supported(data: bytes) -> List[Tuple[int, int]]:
    trailers = _trailers(data)
    for s, e in trailers:
        if b"/Encrypt" in data[s:e]:
            raise UnsupportedFile("encrypted PDF")
    if b"/ObjStm" in data or b"/XRef" in data or not trailers:
        raise UnsupportedFile(
            "PDF uses cross-reference/object streams (PDF 1.5+ compressed structure); "
            "only classic xref tables are supported")
    return trailers


def _stream_span(data: bytes, dict_end: int) -> Optional[Tuple[int, int]]:
    i = _skip_ws(data, dict_end)
    if not data.startswith(b"stream", i):
        return None
    i += 6
    if data.startswith(b"\r\n", i):
        i += 2
    elif data[i:i + 1] in (b"\n", b"\r"):
        i += 1
    j = data.find(b"endstream", i)
    if j < 0:
        raise FormatError("unterminated PDF stream")
    return i, j


def _plan(data: bytes):
    """Return a list of (Item, [(start, end, replacement-bytes)])."""
    trailers = _check_supported(data)
    objs = _objects(data)
    plan = []
    info_refs = set()
    for s, e in trailers:
        m = re.search(rb"/Info\s+(\d+)\s+(\d+)\s+R", data[s:e])
        if m:
            info_refs.add((int(m.group(1)), int(m.group(2))))
    for ref in sorted(info_refs):
        for off in objs.get(ref, []):
            ds = _skip_ws(data, off)
            if not data.startswith(b"<<", ds):
                continue
            keys, de = _dict_keys(data, ds)
            if not keys:
                continue
            edits = [(ds, de, b"<<>>".ljust(de - ds))]
            # Indirect values (e.g. /Author 7 0 R): blank those objects too.
            for r in REF_RE.finditer(data, ds, de):
                for voff in objs.get((int(r.group(1)), int(r.group(2))), []):
                    vs = _skip_ws(data, voff)
                    ve = _object_end(data, vs)
                    edits.append((vs, ve, b"()".ljust(ve - vs)))
            plan.append((Item("Info dictionary", de - ds, True, details=keys), edits))
    meta_objs = set()
    for m in META_REF_RE.finditer(data):
        meta_objs.add((int(m.group(1)), int(m.group(2))))
        plan.append((Item("/Metadata reference", m.end() - m.start(), True),
                     [(m.start(), m.end(), b" " * (m.end() - m.start()))]))
    for ref, offs in objs.items():
        for off in offs:
            ds = _skip_ws(data, off)
            if not data.startswith(b"<<", ds):
                continue
            try:
                _, de = _dict_keys(data, ds)
            except FormatError:
                continue  # 'N G obj' look-alike inside binary data
            if ref not in meta_objs and not META_DICT_RE.search(data, ds, de):
                continue
            span = _stream_span(data, de)
            if span is None:
                continue
            lm = LENGTH_RE.search(data, ds, de)
            new_dict = (b"<</Length " + lm.group(1) + b">>") if lm else b"<<>>"
            ss, se = span
            if len(new_dict) > de - ds:
                continue
            plan.append((Item("XMP metadata stream", se - ss, True),
                         [(ds, de, new_dict.ljust(de - ds)), (ss, se, b" " * (se - ss))]))
    return plan


def scan(data: bytes, opts: Options) -> List[Item]:
    return [item for item, _ in _plan(data)]


def clean(data: bytes, opts: Options) -> bytes:
    out = bytearray(data)
    for _, edits in _plan(data):
        for s, e, rep in edits:
            assert len(rep) == e - s
            out[s:e] = rep
    return bytes(out)


def media_digest(data: bytes) -> Optional[str]:
    return None  # no meaningful payload digest without a full PDF parser
