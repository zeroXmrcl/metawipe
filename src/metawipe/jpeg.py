"""JPEG: drop APPn metadata segments, comments and trailing data."""

from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple

from . import exif
from .core import FormatError, Item, Options, sha256

NAME = "JPEG"

SOI = b"\xff\xd8"
EOI = 0xD9
SOS = 0xDA
COM = 0xFE
STANDALONE = {0x01} | set(range(0xD0, 0xD8))

Segment = Tuple[int, int, int]  # (marker, start, end); end includes entropy data for SOS


def sniff(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def segments(data: bytes) -> Tuple[List[Segment], int]:
    """Split a JPEG into segments. Returns (segments, offset just past EOI)."""
    if data[:2] != SOI:
        raise FormatError("missing JPEG SOI marker")
    segs: List[Segment] = [(0xD8, 0, 2)]
    pos, n = 2, len(data)
    while pos < n:
        if data[pos] != 0xFF:
            raise FormatError(f"expected a JPEG marker at offset {pos}")
        while pos + 1 < n and data[pos + 1] == 0xFF:  # fill bytes
            pos += 1
        if pos + 1 >= n:
            break
        marker, start = data[pos + 1], pos
        if marker == EOI:
            segs.append((EOI, start, pos + 2))
            return segs, pos + 2
        if marker in STANDALONE:
            segs.append((marker, start, pos + 2))
            pos += 2
            continue
        if pos + 4 > n:
            raise FormatError("truncated JPEG segment header")
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        end = pos + 2 + length
        if length < 2 or end > n:
            raise FormatError(f"bad JPEG segment length at offset {pos}")
        if marker == SOS:
            i = end
            while True:
                i = data.find(b"\xff", i)
                if i < 0 or i + 1 >= n:
                    i = n
                    break
                nb = data[i + 1]
                if nb == 0x00 or 0xD0 <= nb <= 0xD7:
                    i += 2
                    continue
                break
            end = i
        segs.append((marker, start, end))
        pos = end
    return segs, n  # no EOI: truncated file, keep what we have


def _payload(data: bytes, seg: Segment) -> bytes:
    marker, start, end = seg
    return data[start + 4:end]


def _classify(data: bytes, seg: Segment, opts: Options) -> Optional[Item]:
    """Return an Item for metadata-ish segments, None for plain image structure."""
    marker, start, end = seg
    size = end - start
    if not (0xE0 <= marker <= 0xEF or marker == COM):
        return None
    p = _payload(data, seg)
    n = marker - 0xE0
    if marker == COM:
        return Item("COM comment", size, True)
    if marker == 0xE0:
        if p.startswith(b"JFIF\x00"):
            return Item("APP0 JFIF", size, False, "needed for rendering")
        if p.startswith(b"JFXX\x00"):
            return Item("APP0 JFXX thumbnail", size, True)
    if marker == 0xE1:
        if p.startswith(b"Exif\x00\x00"):
            info = exif.parse(p)
            if opts.keep_orientation and info.orientation_only:
                return Item("APP1 EXIF (Orientation only)", size, False, "kept by --keep-orientation")
            return Item("APP1 EXIF", size, True, details=info.describe())
        if p.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            return Item("APP1 XMP", size, True)
        if p.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
            return Item("APP1 XMP (extended)", size, True)
    if marker == 0xE2:
        if p.startswith(b"ICC_PROFILE\x00"):
            if opts.strip_icc:
                return Item("APP2 ICC profile", size, True)
            return Item("APP2 ICC profile", size, False, "color profile; use --strip-icc to remove")
        if p.startswith(b"MPF\x00"):
            return Item("APP2 MPF (multi-picture index)", size, True)
    if marker == 0xED and p.startswith(b"Photoshop 3.0\x00"):
        return Item("APP13 Photoshop/IPTC", size, True)
    if marker == 0xEE and p.startswith(b"Adobe"):
        return Item("APP14 Adobe", size, False, "color transform flag, needed for rendering")
    ident = p[:16].split(b"\x00", 1)[0]
    label = ident.decode("ascii", "replace") if ident and all(32 <= c < 127 for c in ident) else ""
    return Item(f"APP{n} {label}".rstrip(), size, True)


def scan(data: bytes, opts: Options) -> List[Item]:
    segs, eoi_end = segments(data)
    items = [it for it in (_classify(data, s, opts) for s in segs) if it is not None]
    if eoi_end < len(data):
        items.append(Item("trailing data after EOI", len(data) - eoi_end, True,
                          "often embedded previews or vendor data"))
    return items


def _orientation(data: bytes, segs: List[Segment]) -> Optional[int]:
    for s in segs:
        if s[0] == 0xE1:
            p = _payload(data, s)
            if p.startswith(b"Exif\x00\x00"):
                o = exif.parse(p).orientation
                if o:
                    return o
    return None


def clean(data: bytes, opts: Options) -> bytes:
    segs, eoi_end = segments(data)
    out = []
    orientation = _orientation(data, segs) if opts.keep_orientation else None
    inserted = orientation is None or orientation == 1
    for s in segs:
        item = _classify(data, s, opts)
        if item is None or not item.remove:
            out.append(data[s[1]:s[2]])
        after_soi = s[0] == 0xD8 and not _next_is_jfif(data, segs)
        after_jfif = item is not None and item.name == "APP0 JFIF"
        if not inserted and (after_soi or after_jfif):
            # EXIF must come right after SOI (or after the JFIF header).
            payload = b"Exif\x00\x00" + exif.build_orientation_only(orientation)
            out.append(b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload)
            inserted = True
    return b"".join(out)


def _next_is_jfif(data: bytes, segs: List[Segment]) -> bool:
    return len(segs) > 1 and segs[1][0] == 0xE0 and _payload(data, segs[1]).startswith(b"JFIF\x00")


def iter_image_segments(data: bytes) -> Iterator[bytes]:
    segs, _ = segments(data)
    for marker, start, end in segs:
        if 0xE0 <= marker <= 0xEF or marker == COM:
            continue
        yield data[start:end]


def media_digest(data: bytes) -> Optional[str]:
    """Hash of everything that is not APPn/COM: tables, frame header, scans."""
    try:
        return sha256(iter_image_segments(data))
    except FormatError:
        return None
