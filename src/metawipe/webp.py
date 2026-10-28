"""WebP (RIFF): drop EXIF and XMP chunks, fix VP8X flags and the RIFF size."""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from . import exif
from .core import FormatError, Item, Options, sha256

NAME = "WebP"

KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
MEDIA = {b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF"}
FLAG_ICC, FLAG_EXIF, FLAG_XMP = 0x20, 0x08, 0x04

Chunk = Tuple[bytes, int, int]  # (fourcc, start, end incl. padding)


def sniff(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def chunks(data: bytes) -> Tuple[List[Chunk], int]:
    if not sniff(data):
        raise FormatError("not a RIFF/WEBP file")
    (riff_size,) = struct.unpack("<I", data[4:8])
    riff_end = min(8 + riff_size, len(data))
    pos, out = 12, []
    while pos + 8 <= riff_end:
        fourcc = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        end = pos + 8 + size + (size & 1)
        if pos + 8 + size > riff_end:
            raise FormatError(f"WebP chunk {fourcc!r} overruns the file")
        out.append((fourcc, pos, min(end, riff_end)))
        pos = end
    return out, 8 + riff_size


def _classify(data: bytes, c: Chunk, opts: Options) -> Optional[Item]:
    fourcc, start, end = c
    size = end - start
    if fourcc == b"EXIF":
        return Item("EXIF chunk", size, True, details=exif.parse(data[start + 8:end]).describe())
    if fourcc == b"XMP ":
        return Item("XMP chunk", size, True)
    if fourcc == b"ICCP":
        if opts.strip_icc:
            return Item("ICCP ICC profile", size, True)
        return Item("ICCP ICC profile", size, False, "color profile; use --strip-icc to remove")
    if fourcc in KEEP:
        return None
    return Item(f"{fourcc.decode('latin-1').strip()} (unknown chunk)", size, True)


def scan(data: bytes, opts: Options) -> List[Item]:
    cs, riff_end = chunks(data)
    items = [it for it in (_classify(data, c, opts) for c in cs) if it is not None]
    if riff_end < len(data):
        items.append(Item("trailing data after RIFF", len(data) - riff_end, True))
    return items


def clean(data: bytes, opts: Options) -> bytes:
    cs, _ = chunks(data)
    kept = []
    for c in cs:
        it = _classify(data, c, opts)
        if it is None or not it.remove:
            kept.append(c)
    types = {c[0] for c in kept}
    body = []
    for fourcc, start, end in kept:
        raw = bytearray(data[start:end])
        if len(raw) % 2:
            raw += b"\x00"
        if fourcc == b"VP8X" and len(raw) >= 9:
            flags = raw[8]
            if b"EXIF" not in types:
                flags &= ~FLAG_EXIF
            if b"XMP " not in types:
                flags &= ~FLAG_XMP
            if b"ICCP" not in types:
                flags &= ~FLAG_ICC
            raw[8] = flags
        body.append(bytes(raw))
    payload = b"WEBP" + b"".join(body)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def media_digest(data: bytes) -> Optional[str]:
    try:
        cs, _ = chunks(data)
    except FormatError:
        return None
    return sha256(data[s:e] for t, s, e in cs if t in MEDIA)
