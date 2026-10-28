"""PNG: drop text, EXIF, time and unknown ancillary chunks."""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from . import exif
from .core import FormatError, Item, Options, sha256

NAME = "PNG"
SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Ancillary chunks that affect how the image is displayed are kept.
KEEP = {
    b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"bKGD", b"pHYs",
    b"cICP", b"mDCv", b"cLLI", b"hIST", b"sPLT",
    b"acTL", b"fcTL", b"fdAT",  # APNG animation
}
METADATA = {
    b"tEXt": "text", b"zTXt": "compressed text", b"iTXt": "international text",
    b"eXIf": "EXIF", b"tIME": "last-modification time",
}
MEDIA = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"acTL", b"fcTL", b"fdAT"}

Chunk = Tuple[bytes, int, int]  # (type, start, end) where end includes the CRC


def sniff(data: bytes) -> bool:
    return data[:8] == SIGNATURE


def chunks(data: bytes) -> Tuple[List[Chunk], int]:
    if data[:8] != SIGNATURE:
        raise FormatError("missing PNG signature")
    pos, n, out = 8, len(data), []
    while pos < n:
        if pos + 8 > n:
            raise FormatError("truncated PNG chunk header")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > n or not ctype.isalpha():
            raise FormatError(f"bad PNG chunk at offset {pos}")
        out.append((ctype, pos, end))
        pos = end
        if ctype == b"IEND":
            return out, pos
    raise FormatError("PNG has no IEND chunk")


def _classify(data: bytes, c: Chunk, opts: Options) -> Optional[Item]:
    ctype, start, end = c
    size = end - start
    name = ctype.decode("ascii")
    body = data[start + 8:end - 4]
    if ctype in METADATA:
        label = f"{name} ({METADATA[ctype]})"
        details: List[str] = []
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            keyword = body.split(b"\x00", 1)[0][:79].decode("latin-1")
            if keyword == "XML:com.adobe.xmp":
                label = f"{name} XMP"
            else:
                details.append(f"keyword: {keyword}")
        elif ctype == b"eXIf":
            details = exif.parse(body).describe()
        return Item(label, size, True, details=details)
    if ctype == b"iCCP":
        if opts.strip_icc:
            return Item("iCCP ICC profile", size, True)
        return Item("iCCP ICC profile", size, False, "color profile; use --strip-icc to remove")
    if ctype in KEEP or ctype[0:1].isupper():
        return None  # critical or rendering-related
    return Item(f"{name} (unknown ancillary chunk)", size, True)


def scan(data: bytes, opts: Options) -> List[Item]:
    cs, end = chunks(data)
    items = [it for it in (_classify(data, c, opts) for c in cs) if it is not None]
    if end < len(data):
        items.append(Item("trailing data after IEND", len(data) - end, True))
    return items


def clean(data: bytes, opts: Options) -> bytes:
    cs, _ = chunks(data)
    out = [SIGNATURE]
    for c in cs:
        it = _classify(data, c, opts)
        if it is None or not it.remove:
            out.append(data[c[1]:c[2]])
    return b"".join(out)


def media_digest(data: bytes) -> Optional[str]:
    try:
        cs, _ = chunks(data)
    except FormatError:
        return None
    return sha256(data[s:e] for t, s, e in cs if t in MEDIA)
