"""Build small sample files with injected metadata, using only the stdlib.

All values are synthetic placeholders; the GPS position is a public
landmark (Eiffel Tower), not anybody's home.
"""

from __future__ import annotations

import struct
import zlib
from typing import List, Tuple

# ---------------------------------------------------------------- EXIF

Entry = Tuple[int, int, int, bytes]


def asc(tag: int, s: str) -> Entry:
    b = s.encode("ascii") + b"\x00"
    return (tag, 2, len(b), b)


def short(tag: int, v: int) -> Entry:
    return (tag, 3, 1, struct.pack("<H", v))


def long_(tag: int, v: int) -> Entry:
    return (tag, 4, 1, struct.pack("<I", v))


def rationals(tag: int, *pairs: Tuple[int, int]) -> Entry:
    return (tag, 5, len(pairs), b"".join(struct.pack("<II", a, b) for a, b in pairs))


def _ifd_size(entries: List[Entry]) -> int:
    return 2 + 12 * len(entries) + 4 + sum(len(p) + len(p) % 2 for *_, p in entries if len(p) > 4)


def _ifd(entries: List[Entry], base: int, next_off: int = 0) -> bytes:
    entries = sorted(entries)
    head = struct.pack("<H", len(entries))
    data = b""
    data_off = base + 2 + 12 * len(entries) + 4
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            val = payload.ljust(4, b"\x00")
        else:
            val = struct.pack("<I", data_off + len(data))
            data += payload + (b"\x00" if len(payload) % 2 else b"")
        head += struct.pack("<HHI", tag, typ, count) + val
    return head + struct.pack("<I", next_off) + data


def build_exif(gps: bool = True, thumbnail: bytes = b"", orientation: int = 1) -> bytes:
    """Little-endian TIFF/EXIF data with typical camera fields."""
    ifd0 = [asc(0x010F, "ExampleCam"), asc(0x0110, "EC-1"), asc(0x0131, "Example Firmware 1.0"),
            asc(0x0132, "2020:01:01 12:00:00"), short(0x0112, orientation), long_(0x8769, 0)]
    exif_ifd = [asc(0x9003, "2020:01:01 12:00:00"), asc(0xA431, "SN-000000")]
    gps_ifd = [(0x0000, 1, 4, bytes([2, 3, 0, 0])), asc(0x0001, "N"),
               rationals(0x0002, (48, 1), (51, 1), (2970, 100)), asc(0x0003, "E"),
               rationals(0x0004, (2, 1), (17, 1), (4020, 100))]
    if gps:
        ifd0.append(long_(0x8825, 0))
    ifd1 = [long_(0x0201, 0), long_(0x0202, len(thumbnail))] if thumbnail else []

    off0 = 8
    off_exif = off0 + _ifd_size(ifd0)
    off_gps = off_exif + _ifd_size(exif_ifd)
    off_ifd1 = off_gps + (_ifd_size(gps_ifd) if gps else 0)
    off_thumb = off_ifd1 + (_ifd_size(ifd1) if ifd1 else 0)

    ifd0 = [e for e in ifd0 if e[0] not in (0x8769, 0x8825)] + [long_(0x8769, off_exif)]
    if gps:
        ifd0.append(long_(0x8825, off_gps))
    if ifd1:
        ifd1 = [long_(0x0201, off_thumb), long_(0x0202, len(thumbnail))]
    out = b"II*\x00" + struct.pack("<I", off0)
    out += _ifd(ifd0, off0, off_ifd1 if ifd1 else 0)
    out += _ifd(exif_ifd, off_exif)
    if gps:
        out += _ifd(gps_ifd, off_gps)
    if ifd1:
        out += _ifd(ifd1, off_ifd1) + thumbnail
    return out


XMP = (b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
       b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:creator="Example Person"/>'
       b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
FAKE_ICC = b"\x00\x00\x00\x80" + b"none" + b"\x02\x10\x00\x00" + b"mntrRGB XYZ " + bytes(112)

# ---------------------------------------------------------------- JPEG


def _huff_codes(bits: List[int], vals: List[int]):
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return codes


DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_VALS = list(range(12))
AC_BITS = [1] + [0] * 15
AC_VALS = [0x00]  # only EOB: every block is flat


def _seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def app(n: int, payload: bytes) -> bytes:
    return _seg(0xE0 + n, payload)


def jpeg_plain(width: int = 32, height: int = 16) -> bytes:
    """A real, decodable baseline grayscale JPEG made of flat 8x8 blocks."""
    assert width % 8 == 0 and height % 8 == 0
    dc = _huff_codes(DC_BITS, DC_VALS)
    ac = _huff_codes(AC_BITS, AC_VALS)
    bits: List[int] = []

    def put(code: int, length: int) -> None:
        bits.extend((code >> (length - 1 - i)) & 1 for i in range(length))

    prev = 0
    bw, bh = width // 8, height // 8
    for by in range(bh):
        for bx in range(bw):
            level = 40 + (bx * 211 // max(bw - 1, 1) + by * 97) % 200
            coef = 8 * (level - 128)
            diff, prev = coef - prev, coef
            cat = abs(diff).bit_length()
            put(*dc[cat])
            if cat:
                put(diff if diff > 0 else diff + (1 << cat) - 1, cat)
            put(*ac[0x00])
    bits.extend([1] * (-len(bits) % 8))
    scan = bytearray()
    for i in range(0, len(bits), 8):
        byte = int("".join(map(str, bits[i:i + 8])), 2)
        scan.append(byte)
        if byte == 0xFF:
            scan.append(0x00)
    return (b"\xff\xd8"
            + app(0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
            + _seg(0xDB, b"\x00" + bytes([1] * 64))
            + _seg(0xC0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")
            + _seg(0xC4, b"\x00" + bytes(DC_BITS) + bytes(DC_VALS))
            + _seg(0xC4, b"\x10" + bytes(AC_BITS) + bytes(AC_VALS))
            + _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + bytes(scan)
            + b"\xff\xd9")


def jpeg_with_metadata(orientation: int = 1, trailing: bool = True) -> bytes:
    base = jpeg_plain()
    thumb = jpeg_plain(8, 8)
    meta = (app(1, b"Exif\x00\x00" + build_exif(thumbnail=thumb, orientation=orientation))
            + app(1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP)
            + app(2, b"ICC_PROFILE\x00\x01\x01" + FAKE_ICC)
            + app(13, b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x08\x1c\x02\x50\x00\x04Anon")
            + _seg(0xFE, b"a comment")
            + app(12, b"Ducky\x00\x01\x00\x04\x00\x00\x00\x3c"))
    jfif_end = 2 + 18
    out = base[:jfif_end] + meta + base[jfif_end:]
    if trailing:
        out += b"VENDORTRAILER" + bytes(16)
    return out

# ---------------------------------------------------------------- PNG


def png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def png_with_metadata() -> bytes:
    w, h = 4, 3
    rows = []
    for y in range(h):
        row = b"\x00" + b"".join(bytes([x * 60, y * 80, 128]) for x in range(w))
        rows.append(row)
    raw = b"".join(rows)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + png_chunk(b"iCCP", b"profile\x00\x00" + zlib.compress(FAKE_ICC))
            + png_chunk(b"gAMA", struct.pack(">I", 45455))
            + png_chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))
            + png_chunk(b"tEXt", b"Author\x00Example Person")
            + png_chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(b"secret comment"))
            + png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP)
            + png_chunk(b"eXIf", build_exif())
            + png_chunk(b"tIME", struct.pack(">HBBBBB", 2020, 1, 1, 12, 0, 0))
            + png_chunk(b"prVt", b"private ancillary data")
            + png_chunk(b"IDAT", zlib.compress(raw))
            + png_chunk(b"IEND", b"")
            + b"trailing")

# ---------------------------------------------------------------- WebP

VP8L_2x2 = bytes.fromhex("2f0140000007508f22d7a3ff8188e87f00")  # real 2x2 lossless bitstream


def riff_chunk(fourcc: bytes, body: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")


def webp_with_metadata() -> bytes:
    vp8x = bytes([0x20 | 0x08 | 0x04, 0, 0, 0]) + (1).to_bytes(3, "little") + (1).to_bytes(3, "little")
    body = (b"WEBP" + riff_chunk(b"VP8X", vp8x) + riff_chunk(b"ICCP", FAKE_ICC)
            + riff_chunk(b"VP8L", VP8L_2x2) + riff_chunk(b"EXIF", build_exif())
            + riff_chunk(b"XMP ", XMP) + riff_chunk(b"ZZZZ", b"odd"))
    return b"RIFF" + struct.pack("<I", len(body)) + body

# ---------------------------------------------------------------- GIF

GIF_HEADER = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff"
GIF_IMAGE = b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00" + b"\x02\x02\x44\x01\x00"
GCE = b"\x21\xf9\x04\x00\x00\x00\x00\x00"


def gif_with_metadata() -> bytes:
    netscape = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    xmp = b"\x21\xff\x0bXMP DataXMP" + bytes([len(XMP[:200])]) + XMP[:200] + b"\x00"
    comment = b"\x21\xfe\x0fmade by someone\x00"
    other = b"\x21\xff\x0bEXAMPLEAPP1\x03abc\x00"
    return GIF_HEADER + netscape + comment + xmp + other + GCE + GIF_IMAGE + b"\x3b" + b"junk"

# ---------------------------------------------------------------- MP4


def box(btype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 8) + btype + body


def full(btype: bytes, version: int, body: bytes) -> bytes:
    return box(btype, bytes([version, 0, 0, 0]) + body)


T = 3786825600  # 2024-01-01 in MP4 epoch seconds (1904-based)
MDAT_PAYLOAD = b"\x00\x00\x00\x10" + b"FAKE-VIDEO-FRAME" * 4


def mp4_with_metadata() -> Tuple[bytes, int]:
    """Return (file bytes, offset of the mdat payload referenced by stco)."""
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    mvhd = full(b"mvhd", 0, struct.pack(">IIII", T, T + 5, 1000, 2000) + bytes(80))
    tkhd = full(b"tkhd", 0, struct.pack(">IIII", T, T + 5, 1, 0) + bytes(68))
    mdhd = full(b"mdhd", 1, struct.pack(">QQIQ", T, T + 5, 1000, 2000) + bytes(4))
    hdlr = full(b"hdlr", 0, b"\x00\x00\x00\x00vide" + bytes(12) + b"VideoHandler\x00")
    udta = box(b"udta", box(b"\xa9xyz", b"\x00\x12\x15\xc7+48.8584+002.2945/")
               + box(b"\xa9day", b"\x00\x0a\x15\xc72024-01-01"))
    meta = full(b"meta", 0, full(b"hdlr", 0, b"\x00\x00\x00\x00mdir" + bytes(12) + b"\x00")
                + box(b"ilst", box(b"\xa9too", box(b"data", b"\x00\x00\x00\x01\x00\x00\x00\x00Lavf"))))

    def build(chunk_offset: int) -> bytes:
        stco = full(b"stco", 0, struct.pack(">II", 1, chunk_offset))
        stbl = box(b"stbl", stco)
        minf = box(b"minf", stbl)
        mdia = box(b"mdia", mdhd + hdlr + minf)
        trak = box(b"trak", tkhd + mdia + box(b"udta", box(b"name", b"track")))
        moov = box(b"moov", mvhd + trak + udta + meta)
        return ftyp + moov + box(b"mdat", MDAT_PAYLOAD)

    probe = build(0)
    offset = len(probe) - len(MDAT_PAYLOAD)
    return build(offset), offset

# ---------------------------------------------------------------- PDF


CONTENT = b"BT /F1 12 Tf 20 100 Td (Hi) Tj ET"


def pdf_with_metadata(xmp: bool = True, extra_trailer: bytes = b"") -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R" + (b" /Metadata 4 0 R" if xmp else b"") + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 7 0 R >>",
        b"<< /Type /Metadata /Subtype /XML /Length " + str(len(XMP)).encode() + b" >>\nstream\n" + XMP + b"\nendstream",
        b"<< /Title (Quarterly \\(draft\\) report) /Author (Example Person) /Creator 6 0 R"
        b" /Producer <4578616d706c65> /CreationDate (D:20200101120000Z) >>",
        b"(Example Editor 1.0)",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(CONTENT), CONTENT),
    ]
    out = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 5 0 R".encode() + extra_trailer
            + f" >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out


def pdf_xref_offsets(data: bytes) -> List[int]:
    i = data.rindex(b"startxref")
    xref = int(data[i + 9:].split()[0])
    lines = data[xref:].split(b"\n")
    count = int(lines[1].split()[1])
    return [int(l[:10]) for l in lines[3:2 + count]]
