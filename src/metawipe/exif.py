"""A tiny, read-only EXIF (TIFF IFD) reader.

It only reports *which* well-known tags are present, never their values
(except Orientation, which is not personal data). That is all `metawipe
inspect` needs, and it keeps private data out of terminal logs.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

IFD0_TAGS: Dict[int, str] = {
    0x010E: "ImageDescription",
    0x010F: "Make",
    0x0110: "Model",
    0x0112: "Orientation",
    0x0131: "Software",
    0x0132: "DateTime",
    0x013B: "Artist",
    0x8298: "Copyright",
}
EXIF_TAGS: Dict[int, str] = {
    0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized",
    0x927C: "MakerNote",
    0x9286: "UserComment",
    0xA420: "ImageUniqueID",
    0xA430: "CameraOwnerName",
    0xA431: "BodySerialNumber",
    0xA434: "LensModel",
}
EXIF_IFD_POINTER = 0x8769
GPS_IFD_POINTER = 0x8825
ORIENTATION = 0x0112

# The "headline" fields `inspect` always answers yes/no for.
HEADLINE = ("Make", "Model", "DateTimeOriginal", "Software")


@dataclass
class ExifInfo:
    tags: Set[str] = field(default_factory=set)
    ifd0_tag_ids: Set[int] = field(default_factory=set)
    gps: bool = False
    gps_tag_count: int = 0
    thumbnail: bool = False
    orientation: Optional[int] = None
    total_tags: int = 0

    @property
    def orientation_only(self) -> bool:
        return self.ifd0_tag_ids == {ORIENTATION} and not self.gps and not self.thumbnail

    def describe(self) -> List[str]:
        out = []
        out.append(f"GPS: yes ({self.gps_tag_count} tags)" if self.gps else "GPS: no")
        for name in HEADLINE:
            out.append(f"{name}: {'yes' if name in self.tags else 'no'}")
        extra = sorted(self.tags - set(HEADLINE) - {"Orientation"})
        if extra:
            out.append("also: " + ", ".join(extra))
        if self.orientation is not None:
            out.append(f"Orientation: {self.orientation}")
        out.append(f"embedded thumbnail: {'yes' if self.thumbnail else 'no'}")
        out.append(f"tags total: {self.total_tags}")
        return out


def strip_exif_header(data: bytes) -> bytes:
    """Accept TIFF data with or without the JPEG-style 'Exif\\0\\0' prefix."""
    if data.startswith(b"Exif\x00\x00"):
        return data[6:]
    return data


def parse(data: bytes) -> ExifInfo:
    """Parse TIFF-structured EXIF data. Malformed input yields partial info."""
    tiff = strip_exif_header(data)
    info = ExifInfo()
    if len(tiff) < 8:
        return info
    if tiff[:2] == b"II":
        e = "<"
    elif tiff[:2] == b"MM":
        e = ">"
    else:
        return info
    if struct.unpack(e + "H", tiff[2:4])[0] != 42:
        return info
    seen: Set[int] = set()

    def read_ifd(off: int):
        """Yield (tag, type, count, raw value field) for one IFD; return next offset."""
        entries = []
        if off in seen or off < 8 or off + 2 > len(tiff):
            return entries, 0
        seen.add(off)
        (count,) = struct.unpack(e + "H", tiff[off:off + 2])
        pos = off + 2
        for _ in range(min(count, 1000)):
            if pos + 12 > len(tiff):
                break
            tag, typ, cnt = struct.unpack(e + "HHI", tiff[pos:pos + 8])
            entries.append((tag, typ, cnt, tiff[pos + 8:pos + 12]))
            pos += 12
        nxt = struct.unpack(e + "I", tiff[pos:pos + 4])[0] if pos + 4 <= len(tiff) else 0
        return entries, nxt

    ifd0_off = struct.unpack(e + "I", tiff[4:8])[0]
    ifd0, ifd1_off = read_ifd(ifd0_off)
    for tag, typ, cnt, val in ifd0:
        info.total_tags += 1
        info.ifd0_tag_ids.add(tag)
        if tag in IFD0_TAGS:
            info.tags.add(IFD0_TAGS[tag])
        if tag == ORIENTATION and typ == 3:
            info.orientation = struct.unpack(e + "H", val[:2])[0]
        elif tag == EXIF_IFD_POINTER:
            sub, _ = read_ifd(struct.unpack(e + "I", val)[0])
            for stag, _, _, _ in sub:
                info.total_tags += 1
                if stag in EXIF_TAGS:
                    info.tags.add(EXIF_TAGS[stag])
        elif tag == GPS_IFD_POINTER:
            sub, _ = read_ifd(struct.unpack(e + "I", val)[0])
            info.gps = True
            info.gps_tag_count = len(sub)
            info.total_tags += len(sub)
    if ifd1_off:
        ifd1, _ = read_ifd(ifd1_off)
        if ifd1:
            info.thumbnail = True
            info.total_tags += len(ifd1)
    return info


def build_orientation_only(orientation: int) -> bytes:
    """Return minimal big-endian TIFF/EXIF data holding just the Orientation tag."""
    return (
        b"MM\x00\x2a" + struct.pack(">I", 8)
        + struct.pack(">H", 1)
        + struct.pack(">HHI", ORIENTATION, 3, 1) + struct.pack(">HH", orientation, 0)
        + struct.pack(">I", 0)
    )
