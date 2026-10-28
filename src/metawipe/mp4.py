"""MP4 / MOV (ISO BMFF / QuickTime).

Metadata boxes are never cut out. They are overwritten in place with a
``free`` box of exactly the same size, so every chunk offset (stco/co64)
and the overall file layout stay valid without rewriting the sample tables.
"""

from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple

from .core import FormatError, Item, Options, UnsupportedFile, sha256

NAME = "MP4/MOV"

TOP_LEVEL = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid", b"meta", b"moof", b"mfra", b"styp", b"sidx"}
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"edts", b"dinf", b"stbl", b"mvex"}
STRIP_IN_MOOV = {b"udta": "user data (udta)", b"meta": "metadata (meta)"}
TIMES = {b"mvhd": "movie", b"tkhd": "track", b"mdhd": "media"}
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
VISUAL_ENTRIES = {b"avc1", b"avc3", b"hvc1", b"hev1", b"dvh1", b"dvhe", b"mp4v", b"av01", b"vp08", b"vp09",
                  b"jpeg", b"mjpa", b"mjpb", b"apch", b"apcn", b"apcs", b"apco", b"ap4h", b"ap4x", b"s263", b"h263"}
COMPRESSOR_NAME_OFFSET = 42  # within a VisualSampleEntry, after the 8-byte box header
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"}

Box = Tuple[bytes, int, int, int]  # (type, start, header length, end)


def sniff(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")


def iter_boxes(data: bytes, start: int, end: int) -> Iterator[Box]:
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                raise FormatError("truncated 64-bit box header")
            (size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise FormatError(f"bad box size for {btype!r} at offset {pos}")
        yield btype, pos, hdr, pos + size
        pos += size


def _check_supported(data: bytes) -> List[Box]:
    top = list(iter_boxes(data, 0, len(data)))
    for btype, s, h, e in top:
        if btype == b"ftyp":
            major = data[s + h:s + h + 4]
            compat = {data[i:i + 4] for i in range(s + h + 8, e - 3, 4)}
            if major == b"crx ":
                raise UnsupportedFile("Canon CR3 raw files are not supported")
            if major in HEIF_BRANDS or (b"moov" not in {b[0] for b in top} and compat & HEIF_BRANDS):
                raise UnsupportedFile("HEIF/HEIC/AVIF images are not supported yet")
    return top


def _walk(data: bytes, top: List[Box]):
    """Yield (box, path) for everything worth looking at."""
    def rec(box: Box, path: str):
        btype, s, h, e = box
        yield box, path
        if btype in CONTAINERS:
            for child in iter_boxes(data, s + h, e):
                yield from rec(child, path + "/" + child[0].decode("latin-1"))
    for box in top:
        yield from rec(box, box[0].decode("latin-1"))


def _time_fields(data: bytes, box: Box) -> Tuple[int, int, int]:
    """Return (offset, width, count) of the creation+modification time fields."""
    _, s, h, _ = box
    version = data[s + h]
    return (s + h + 4, 8 if version == 1 else 4, 2)


def _handler(data: bytes, trak: Box) -> Optional[bytes]:
    for box, _ in _walk(data, [trak]):
        if box[0] == b"hdlr":
            s, h = box[1], box[2]
            return data[s + h + 8:s + h + 12]
    return None


def _all_zero(b: bytes) -> bool:
    return not b.strip(b"\x00")


def _findings(data: bytes):
    """Yield (Item, action) where action is ('free', box) or ('zero', offset, length)."""
    top = _check_supported(data)
    for box, path in _walk(data, top):
        btype, s, h, e = box
        size = e - s
        in_moov = path.startswith("moov")
        if in_moov and btype in STRIP_IN_MOOV:
            yield Item(f"{path} {STRIP_IN_MOOV[btype]}", size, True), ("free", box)
            continue
        if not in_moov and btype == b"meta":
            yield Item(f"{path} top-level metadata (meta)", size, True), ("free", box)
        elif not in_moov and btype == b"uuid" and data[s + h:s + h + 16] == XMP_UUID:
            yield Item(f"{path} XMP (uuid)", size, True), ("free", box)
        elif btype == b"udta" and not in_moov:
            yield Item(f"{path} user data (udta)", size, True), ("free", box)
        elif btype in TIMES and in_moov:
            off, width, count = _time_fields(data, box)
            raw = data[off:off + width * count]
            if not _all_zero(raw):
                yield Item(f"{path} {TIMES[btype]} creation/modification time", width * count, True), ("zero", off, width * count)
        elif btype in (b"free", b"skip") and not _all_zero(data[s + h:e]):
            yield Item(f"{path} non-empty padding", size, True, "free/skip boxes can hold leftover data"), ("zero", s + h, size - h)
        elif btype == b"stsd" and in_moov:
            # The 32-byte compressorname of a video sample entry often names the encoder.
            for entry in iter_boxes(data, s + h + 8, e):
                if entry[0] in VISUAL_ENTRIES:
                    off = entry[1] + entry[2] + COMPRESSOR_NAME_OFFSET
                    if off + 32 <= entry[3] and not _all_zero(data[off:off + 32]):
                        yield (Item(f"{path}/{entry[0].decode('latin-1')} compressor name", 32, True),
                               ("zero", off, 32))
        elif btype == b"trak":
            hd = _handler(data, box)
            if hd in (b"meta", b"camm"):
                yield Item(f"{path} timed metadata track ({hd.decode()})", size, False,
                           "warning: may contain GPS/sensor telemetry; media tracks are not removed"), None


def scan(data: bytes, opts: Options) -> List[Item]:
    return [item for item, _ in _findings(data)]


def _free_header(box: Box) -> bytes:
    btype, s, h, e = box
    if h == 16:
        return struct.pack(">I4sQ", 1, b"free", e - s)
    return struct.pack(">I4s", e - s, b"free")


def clean(data: bytes, opts: Options) -> bytes:
    out = bytearray(data)
    actions = [a for item, a in _findings(data) if item.remove and a is not None]
    for a in actions:
        if a[0] == "free":
            _, s, h, e = a[1]
            out[s:e] = _free_header(a[1]) + b"\x00" * (e - s - h)
        else:
            _, off, length = a
            out[off:off + length] = b"\x00" * length
    if len(out) != len(data):
        raise AssertionError("MP4 cleaning must not change the file size")
    return bytes(out)


def media_digest(data: bytes) -> Optional[str]:
    try:
        top = list(iter_boxes(data, 0, len(data)))
    except FormatError:
        return None
    return sha256(data[s + h:e] for t, s, h, e in top if t == b"mdat")
