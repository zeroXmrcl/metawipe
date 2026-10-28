import struct
import unittest

from metawipe import exif, jpeg, mp4, png, webp
from metawipe.core import Options, UnsupportedFile, inspect_bytes, verify_bytes, wipe_bytes

from . import samples as S


def names(items, removable=True):
    return [i.name for i in items if i.remove == removable]


class Common:
    """Mixin: every format must wipe, verify, keep media identical and be idempotent."""

    def sample(self) -> bytes:
        raise NotImplementedError

    def test_wipe_verifies_and_is_idempotent(self):
        data = self.sample()
        res = wipe_bytes(data)
        self.assertTrue(res.removed, "sample should contain metadata")
        v = verify_bytes(data, res.cleaned)
        self.assertTrue(v.ok, v.remaining)
        self.assertNotEqual(v.media_identical, False)
        again = wipe_bytes(res.cleaned)
        self.assertEqual(again.removed, [])
        self.assertEqual(again.cleaned, res.cleaned)


class TestExif(unittest.TestCase):
    def test_reader_reports_presence_only(self):
        info = exif.parse(S.build_exif(thumbnail=b"\xff\xd8\xff\xd9", orientation=6))
        self.assertTrue(info.gps)
        self.assertEqual(info.gps_tag_count, 5)
        self.assertTrue({"Make", "Model", "DateTimeOriginal", "Software"} <= info.tags)
        self.assertTrue(info.thumbnail)
        self.assertEqual(info.orientation, 6)
        text = " ".join(info.describe())
        self.assertIn("GPS: yes", text)
        self.assertNotIn("ExampleCam", text)  # values are never reported

    def test_garbage_does_not_crash(self):
        for blob in (b"", b"II*\x00\xff\xff\xff\xff", b"MM\x00*\x00\x00\x00\x08\xff\xff", b"Exif\x00\x00junk"):
            exif.parse(blob)

    def test_orientation_only_roundtrip(self):
        info = exif.parse(exif.build_orientation_only(8))
        self.assertEqual(info.orientation, 8)
        self.assertTrue(info.orientation_only)


class TestJPEG(Common, unittest.TestCase):
    def sample(self):
        return S.jpeg_with_metadata()

    def test_removes_metadata_keeps_image(self):
        data = self.sample()
        res = wipe_bytes(data)
        self.assertIn("APP1 EXIF", names(res.items))
        self.assertIn("APP1 XMP", names(res.items))
        self.assertIn("APP13 Photoshop/IPTC", names(res.items))
        self.assertIn("COM comment", names(res.items))
        self.assertIn("trailing data after EOI", names(res.items))
        for secret in (b"ExampleCam", b"Example Person", b"a comment", b"VENDORTRAILER", b"Ducky"):
            self.assertNotIn(secret, res.cleaned)
        self.assertIn(b"ICC_PROFILE", res.cleaned)  # kept by default
        self.assertIn(b"JFIF", res.cleaned)
        # Everything that is not APPn/COM is byte-identical, in order.
        self.assertEqual(list(jpeg.iter_image_segments(data)), list(jpeg.iter_image_segments(res.cleaned)))
        self.assertTrue(res.cleaned.endswith(b"\xff\xd9"))

    def test_plain_jpeg_is_left_alone(self):
        data = S.jpeg_plain()
        res = wipe_bytes(data)
        self.assertEqual(res.removed, [])
        self.assertEqual(res.cleaned, data)

    def test_strip_icc(self):
        res = wipe_bytes(self.sample(), Options(strip_icc=True))
        self.assertNotIn(b"ICC_PROFILE", res.cleaned)

    def test_keep_orientation(self):
        data = S.jpeg_with_metadata(orientation=6)
        opts = Options(keep_orientation=True)
        res = wipe_bytes(data, opts)
        segs, _ = jpeg.segments(res.cleaned)
        exifs = [s for s in segs if s[0] == 0xE1]
        self.assertEqual(len(exifs), 1)
        info = exif.parse(res.cleaned[exifs[0][1] + 4:exifs[0][2]])
        self.assertEqual(info.orientation, 6)
        self.assertTrue(info.orientation_only)
        self.assertNotIn(b"ExampleCam", res.cleaned)
        self.assertTrue(verify_bytes(data, res.cleaned, opts).ok)
        # The orientation block sits right after JFIF.
        self.assertEqual([s[0] for s in segs[:3]], [0xD8, 0xE0, 0xE1])

    def test_entropy_data_with_ff00_and_rst(self):
        # Hand-built scan containing stuffed FF00 and an RST marker must survive intact.
        base = S.jpeg_plain()
        sos = base.index(b"\xff\xda")
        hdr_end = sos + 2 + struct.unpack(">H", base[sos + 2:sos + 4])[0]
        weird = base[:hdr_end] + b"\x12\xff\x00\x34\xff\xd0\x56" + b"\xff\xd9"
        segs, end = jpeg.segments(weird)
        self.assertEqual(segs[-2][0], 0xDA)
        self.assertEqual(weird[segs[-2][1]:segs[-2][2]], weird[sos:-2])


class TestPNG(Common, unittest.TestCase):
    def sample(self):
        return S.png_with_metadata()

    def test_chunks(self):
        data = self.sample()
        res = wipe_bytes(data)
        out_types = [c[0] for c in png.chunks(res.cleaned)[0]]
        self.assertEqual(out_types, [b"IHDR", b"iCCP", b"gAMA", b"pHYs", b"IDAT", b"IEND"])
        for secret in (b"Example Person", b"ExampleCam", b"prVt", b"trailing"):
            self.assertNotIn(secret, res.cleaned)
        idat = lambda d: [d[s:e] for t, s, e in png.chunks(d)[0] if t == b"IDAT"]
        self.assertEqual(idat(data), idat(res.cleaned))

    def test_strip_icc(self):
        res = wipe_bytes(self.sample(), Options(strip_icc=True))
        self.assertNotIn(b"iCCP", [c[0] for c in png.chunks(res.cleaned)[0]])


class TestWebP(Common, unittest.TestCase):
    def sample(self):
        return S.webp_with_metadata()

    def test_flags_and_size(self):
        res = wipe_bytes(self.sample())
        out = res.cleaned
        self.assertEqual(struct.unpack("<I", out[4:8])[0], len(out) - 8)
        types = [c[0] for c in webp.chunks(out)[0]]
        self.assertEqual(types, [b"VP8X", b"ICCP", b"VP8L"])
        flags = out[20]
        self.assertFalse(flags & webp.FLAG_EXIF)
        self.assertFalse(flags & webp.FLAG_XMP)
        self.assertTrue(flags & webp.FLAG_ICC)
        self.assertIn(S.VP8L_2x2, out)

    def test_strip_icc_clears_flag(self):
        out = wipe_bytes(self.sample(), Options(strip_icc=True)).cleaned
        self.assertFalse(out[20] & webp.FLAG_ICC)
        self.assertEqual([c[0] for c in webp.chunks(out)[0]], [b"VP8X", b"VP8L"])


class TestGIF(Common, unittest.TestCase):
    def sample(self):
        return S.gif_with_metadata()

    def test_keeps_loop_drops_rest(self):
        out = wipe_bytes(self.sample()).cleaned
        self.assertEqual(out, S.GIF_HEADER + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
                         + S.GCE + S.GIF_IMAGE + b"\x3b")


class TestMP4(Common, unittest.TestCase):
    def sample(self):
        return S.mp4_with_metadata()[0]

    def test_offsets_and_times(self):
        data, mdat_off = S.mp4_with_metadata()
        out = wipe_bytes(data).cleaned
        self.assertEqual(len(out), len(data))
        self.assertEqual(out[mdat_off:mdat_off + len(S.MDAT_PAYLOAD)], S.MDAT_PAYLOAD)
        stco = out.index(b"stco")
        self.assertEqual(struct.unpack(">I", out[stco + 12:stco + 16])[0], mdat_off)
        for secret in (b"+48.8584", b"2024-01-01", b"Lavf", b"udta", b"ilst"):
            self.assertNotIn(secret, out)
        self.assertNotIn(struct.pack(">I", S.T), out)
        top = [b[0] for b in mp4.iter_boxes(out, 0, len(out))]
        self.assertEqual(top, [b"ftyp", b"moov", b"mdat"])

    def test_heif_is_refused(self):
        heic = S.box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + S.box(b"meta", b"\x00" * 8)
        with self.assertRaises(UnsupportedFile):
            inspect_bytes(heic)

    def test_timed_metadata_track_warns(self):
        data, _ = S.mp4_with_metadata()
        data = data.replace(b"vide", b"meta", 1)
        items = inspect_bytes(data).items
        warn = [i for i in items if i.note.startswith("warning")]
        self.assertEqual(len(warn), 1)
        self.assertFalse(warn[0].remove)


class TestPDF(Common, unittest.TestCase):
    def sample(self):
        return S.pdf_with_metadata()

    def test_offsets_stay_valid(self):
        data = self.sample()
        out = wipe_bytes(data).cleaned
        self.assertEqual(len(out), len(data))
        for i, off in enumerate(S.pdf_xref_offsets(out), 1):
            self.assertTrue(out[off:].startswith(f"{i} 0 obj".encode()))
        for secret in (b"Example Person", b"Example Editor", b"Quarterly", b"xpacket", b"/Metadata 4 0 R",
                       b"4578616d706c65"):
            self.assertNotIn(secret, out)
        self.assertIn(b"/Info 5 0 R", out)
        self.assertIn(S.CONTENT, out)  # page content untouched

    def test_info_keys_listed(self):
        items = inspect_bytes(self.sample()).items
        info = [i for i in items if i.name == "Info dictionary"][0]
        self.assertEqual(info.details, ["/Title", "/Author", "/Creator", "/Producer", "/CreationDate"])

    def test_encrypted_is_refused(self):
        data = S.pdf_with_metadata(extra_trailer=b" /Encrypt 9 0 R")
        with self.assertRaises(UnsupportedFile) as cm:
            wipe_bytes(data)
        self.assertIn("encrypted", str(cm.exception))

    def test_xref_stream_is_refused(self):
        data = b"%PDF-1.5\n1 0 obj\n<< /Type /XRef /Size 1 /W [1 2 1] >>\nstream\n\nendstream\nendobj\n" \
               b"startxref\n9\n%%EOF\n"
        with self.assertRaises(UnsupportedFile):
            wipe_bytes(data)


class TestDetect(unittest.TestCase):
    def test_unknown(self):
        with self.assertRaises(UnsupportedFile):
            wipe_bytes(b"hello world")


if __name__ == "__main__":
    unittest.main()
