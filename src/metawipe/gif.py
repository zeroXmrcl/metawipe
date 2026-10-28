"""GIF: drop comment and application extensions (except animation loop info)."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .core import FormatError, Item, Options, sha256

NAME = "GIF"
LOOP_APPS = {b"NETSCAPE2.0", b"ANIMEXTS1.0"}

Block = Tuple[str, int, int, bytes]  # (kind, start, end, label/app id)


def sniff(data: bytes) -> bool:
    return data[:6] in (b"GIF87a", b"GIF89a")


def _subblocks_end(data: bytes, pos: int) -> int:
    n = len(data)
    while True:
        if pos >= n:
            raise FormatError("truncated GIF data sub-blocks")
        size = data[pos]
        pos += 1 + size
        if size == 0:
            return pos


def blocks(data: bytes) -> Tuple[List[Block], int]:
    if not sniff(data) or len(data) < 13:
        raise FormatError("not a GIF file")
    flags = data[10]
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    out: List[Block] = [("header", 0, pos, b"")]
    n = len(data)
    while pos < n:
        b = data[pos]
        if b == 0x3B:
            out.append(("trailer", pos, pos + 1, b""))
            return out, pos + 1
        if b == 0x2C:
            if pos + 10 > n:
                raise FormatError("truncated GIF image descriptor")
            lflags = data[pos + 9]
            p = pos + 10 + (3 * (2 << (lflags & 7)) if lflags & 0x80 else 0)
            end = _subblocks_end(data, p + 1)  # skip LZW minimum code size
            out.append(("image", pos, end, b""))
            pos = end
        elif b == 0x21:
            if pos + 2 > n:
                raise FormatError("truncated GIF extension")
            label = data[pos + 1]
            ident = b""
            if label == 0xFF and pos + 3 < n:
                ident = data[pos + 3:pos + 3 + data[pos + 2]]
            end = _subblocks_end(data, pos + 2)
            out.append((f"ext{label:02x}", pos, end, ident))
            pos = end
        else:
            raise FormatError(f"unexpected GIF block 0x{b:02x} at offset {pos}")
    return out, n  # missing trailer


def _classify(blk: Block, opts: Options) -> Optional[Item]:
    kind, start, end, ident = blk
    size = end - start
    if kind == "extfe":
        return Item("comment extension", size, True)
    if kind == "extff":
        name = ident.decode("latin-1", "replace")
        if ident in LOOP_APPS:
            return Item(f"application extension {name}", size, False, "animation loop count")
        if ident == b"ICCRGBG1":
            if opts.strip_icc:
                return Item("application extension ICC profile", size, True)
            return Item("application extension ICC profile", size, False, "color profile; use --strip-icc to remove")
        if ident == b"XMP DataXMP":
            return Item("application extension XMP", size, True)
        return Item(f"application extension {name}", size, True)
    if kind.startswith("ext") and kind not in ("extf9", "ext01"):
        return Item(f"unknown extension 0x{kind[3:]}", size, True)
    return None


def scan(data: bytes, opts: Options) -> List[Item]:
    bs, end = blocks(data)
    items = [it for it in (_classify(b, opts) for b in bs) if it is not None]
    if end < len(data):
        items.append(Item("trailing data after GIF trailer", len(data) - end, True))
    return items


def clean(data: bytes, opts: Options) -> bytes:
    bs, _ = blocks(data)
    out = []
    for b in bs:
        it = _classify(b, opts)
        if it is None or not it.remove:
            out.append(data[b[1]:b[2]])
    return b"".join(out)


def media_digest(data: bytes) -> Optional[str]:
    try:
        bs, _ = blocks(data)
    except FormatError:
        return None
    return sha256(data[s:e] for k, s, e, _ in bs if k in ("header", "image", "extf9", "ext01"))
