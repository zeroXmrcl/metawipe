# metawipe

**Strip metadata from photos, videos and PDFs, then prove it's gone.**

[![CI](https://github.com/zeroXmrcl/metawipe/actions/workflows/ci.yml/badge.svg)](https://github.com/zeroXmrcl/metawipe/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.9-3.13](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)

`metawipe` removes EXIF (including GPS), XMP, IPTC, comments, timestamps and other
metadata from JPEG, PNG, WebP, GIF, MP4/MOV and (simple) PDF files. It then re-reads
every output file and checks two things: no known metadata is left, and the actual
image or video data is byte-for-byte unchanged.

- **Verifies its own work.** `--verify` re-parses the written file and exits with code 1
  if anything removable is still there. It also compares a SHA-256 of the media payload
  (JPEG scans, PNG `IDAT`, WebP bitstream, GIF frames, MP4 `mdat`) before and after.
- **Zero dependencies.** Pure Python standard library, a single `pip`/`pipx` install,
  runs anywhere Python 3.9+ runs.
- **Safe by default.** Originals are never touched unless you pass `--in-place`.
  The default writes `photo.clean.jpg` next to `photo.jpg`.
- **No re-encoding.** Pixels and video frames are never decoded or recompressed, so
  there is no quality loss.
- **Doesn't leak what it finds.** `inspect` reports block names, sizes and yes/no flags
  ("GPS: yes"), never the values, so it is safe to paste into an issue or a CI log.

## Install

metawipe is not on PyPI yet. Install it straight from GitHub:

```sh
pipx install git+https://github.com/zeroXmrcl/metawipe
# or
pip install git+https://github.com/zeroXmrcl/metawipe
```

You can also run it from a checkout without installing: `PYTHONPATH=src python -m metawipe --help`.

## Usage

```console
$ metawipe inspect vacation.jpg
vacation.jpg  [JPEG, 22.6 KB]
  keep    APP0 JFIF (18 B)  [needed for rendering]
  remove  APP1 EXIF (308 B)
          GPS: yes (4 tags) | Make: yes | Model: yes | DateTimeOriginal: yes | Software: yes | also: DateTime | Orientation: 1 | embedded thumbnail: no | tags total: 12
  remove  COM comment (19 B)

$ metawipe --verify vacation.jpg clip.mp4 letter.pdf
vacation.jpg  [JPEG]  ->  vacation.clean.jpg
  removed  APP1 EXIF (308 B)
           GPS: yes (4 tags) | Make: yes | Model: yes | DateTimeOriginal: yes | Software: yes | also: DateTime | Orientation: 1 | embedded thumbnail: no | tags total: 12
  removed  COM comment (19 B)
  kept     APP0 JFIF (18 B)  [needed for rendering]
  verify   OK: no known metadata left, media data identical
clip.mp4  [MP4/MOV]  ->  clip.clean.mp4
  removed  moov/mvhd movie creation/modification time (8 B)
  removed  moov/trak/tkhd track creation/modification time (8 B)
  removed  moov/trak/mdia/mdhd media creation/modification time (8 B)
  removed  moov/trak/mdia/minf/stbl/stsd/avc1 compressor name (32 B)
  removed  moov/trak/tkhd track creation/modification time (8 B)
  removed  moov/trak/mdia/mdhd media creation/modification time (8 B)
  removed  moov/udta user data (udta) (388 B)
  verify   OK: no known metadata left, media data identical
letter.pdf  [PDF]  ->  letter.clean.pdf
  removed  Info dictionary (307 B)
           /Creator | /Producer | /CreationDate
  removed  /Metadata reference (16 B)
  removed  XMP metadata stream (4.9 KB)
  verify   OK: no known metadata left, media digest n/a for this format
summary: 3 cleaned, 0 errors; verify: 3 passed, 0 failed

$ metawipe inspect --check vacation.clean.jpg && echo "no metadata left"
vacation.clean.jpg  [JPEG, 22.2 KB]
  keep    APP0 JFIF (18 B)  [needed for rendering]
no metadata left
```

More examples:

```sh
metawipe -n photo.jpg                    # dry run: show what would be removed
metawipe -r -o cleaned/ ~/Pictures/trip  # clean a folder tree into cleaned/, same layout
metawipe --in-place --backup *.png       # overwrite originals, keep FILE.bak copies
metawipe --strip-icc image.webp          # also drop the embedded color profile
metawipe --keep-orientation photo.jpg    # keep only the EXIF Orientation tag
metawipe --json --verify -r uploads/     # machine-readable report
metawipe inspect --check uploads/*.jpg   # CI/pre-commit: exit 1 if metadata is present
```

### Options

| Option | Meaning |
| --- | --- |
| `-r`, `--recursive` | Process directories recursively (unsupported files are skipped, `*.clean.*` and `*.bak` are ignored). |
| `-n`, `--dry-run` | Report what would be removed, write nothing. |
| `--verify` | Re-read every output and exit 1 if known metadata remains or the media payload changed. |
| `-o DIR`, `--output DIR` | Write cleaned copies into `DIR` (mirrors the folder structure with `-r`). |
| `--in-place` | Overwrite originals. Add `--backup` to keep `FILE.bak`. |
| `-f`, `--force` | Overwrite existing output or backup files. |
| `--strip-icc` | Also remove ICC color profiles (kept by default because removing them can shift colors). |
| `--keep-orientation` | JPEG: keep a minimal EXIF block containing only the Orientation tag. |
| `-q`, `--quiet` | Only print errors and verification failures. |
| `--json` | Print a JSON report. |
| `--version` | Print the version. |

`metawipe inspect [--json] [--check] FILE...` lists what is in a file without changing it.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. For `inspect --check`: no removable metadata found. |
| 1 | `--verify` found metadata left in an output (or the media payload changed). For `inspect --check`: metadata found. |
| 2 | Usage error (bad options, directory without `-r`). |
| 3 | At least one file could not be processed (unsupported, damaged, I/O error). |

## Formats and limitations

Files are detected by their content (magic bytes), not by extension.

| Format | Removed | Kept, and why |
| --- | --- | --- |
| JPEG | APP1 EXIF (incl. GPS, maker notes, thumbnail), APP1 XMP, APP13 IPTC/Photoshop, COM comments, MPF index, any other APPn segment, data after the end-of-image marker | APP0 JFIF and APP14 Adobe (needed to decode colors correctly), APP2 ICC profile unless `--strip-icc` |
| PNG | `tEXt`, `zTXt`, `iTXt` (incl. XMP), `eXIf`, `tIME`, unknown ancillary chunks (e.g. vendor or C2PA chunks), data after `IEND` | Image and rendering chunks: `IHDR`, `PLTE`, `IDAT`, `IEND`, `tRNS`, `gAMA`, `cHRM`, `sRGB`, `sBIT`, `bKGD`, `pHYs`, `cICP`, APNG chunks; `iCCP` unless `--strip-icc` |
| WebP | `EXIF` and `XMP ` chunks, unknown chunks, trailing data; VP8X flags and RIFF size are fixed up | Bitstream, alpha and animation chunks; `ICCP` unless `--strip-icc` |
| GIF | Comment extensions, application extensions (incl. XMP), unknown extensions, data after the trailer | Frames, graphic control (timing, transparency), plain text, `NETSCAPE2.0`/`ANIMEXTS1.0` loop count, ICC unless `--strip-icc` |
| MP4 / MOV | `udta` and `meta` boxes in `moov` and its tracks (title, artist, GPS `©xyz`, encoder tags), top-level `meta`/`udta`/XMP `uuid` boxes, creation/modification times in `mvhd`/`tkhd`/`mdhd` (zeroed), video encoder name in the sample description (zeroed), leftover bytes in `free`/`skip` padding (zeroed) | All media data. Removed boxes are overwritten in place by `free` boxes of the same size, so chunk offsets (`stco`/`co64`) stay valid and the file size does not change |
| PDF (best effort) | Every Info dictionary entry (Title, Author, Creator, Producer, dates, ...) including indirect string values, `/Metadata` references and XMP metadata streams | Everything else. Edits are same-length overwrites, so the xref table stays valid |

What metawipe does **not** do (please read this before relying on it):

- **Pixel content is untouched.** Faces, street signs, screen contents or reflections in
  the image itself can still reveal who or where you are.
- **Filesystem metadata is not managed.** Owner, permissions and timestamps belong to
  the file system, not the file. New copies get the current time; `--in-place` rewrites
  get a new modification time too.
- **Orientation:** removing EXIF also removes the Orientation tag, so a photo taken
  sideways may display rotated. metawipe warns about this; use `--keep-orientation` for JPEG.
- **PDF support is best effort.** Only PDFs with a classic `xref` table are handled.
  Files using cross-reference streams or object streams (common for PDF 1.5+), and
  encrypted files, are refused with a clear message instead of being half-cleaned. The
  old Info data is overwritten, not just unreferenced, but metawipe does not parse page
  content, annotations, form fields, attachments or document-level JavaScript, and the
  trailer `/ID` (a file identifier) is kept.
- **Video:** metadata inside the compressed bitstream (for example x264 writes its
  encoder settings into an SEI message) and timed-metadata tracks (GoPro GPMF, camera
  motion `camm`) are media, not container metadata, and are not removed. metawipe
  prints a warning when it sees a timed-metadata track.
- **Not supported (yet):** HEIC/HEIF/AVIF (refused), camera RAW, TIFF, audio-only
  files, Office documents, SVG.
- Files are processed in memory, so very large videos need a matching amount of RAM.
- "No known metadata left" means none of the blocks listed above remain. It is a
  strong check for these formats, not a proof against every exotic vendor extension.

## How it compares

| | metawipe | [ExifTool](https://exiftool.org/) | [mat2](https://0xacab.org/jvoisin/mat2) |
| --- | --- | --- | --- |
| Formats | 6 (see above) | Far more, incl. RAW, HEIC, TIFF | Many, incl. Office, audio, archives |
| Verification of the output | Built in (`--verify`, media hash) | Separate step: re-read the file with `exiftool` | Separate step: `mat2 --show` on the output |
| Dependencies | None (Python stdlib) | Perl | Python plus system libraries (e.g. Poppler, GdkPixbuf, ffmpeg) |
| Default behavior | Writes a `.clean` copy | Writes in place, keeps `_original` | Writes a `.cleaned` copy |
| Maturity | New (0.1.0) | Two decades, the reference tool | Mature, privacy-focused |

If you need broad format coverage, use ExifTool (`exiftool -all= file`) or mat2. metawipe
is for the common case: photos, screen recordings and simple PDFs you are about to share,
with an install that has no moving parts and a check you can put in CI.

## Development

```sh
git clone https://github.com/zeroXmrcl/metawipe
cd metawipe
python -m unittest discover -s tests -t . -v
```

The tests build every sample file from scratch in `tests/samples.py` (including a tiny
baseline JPEG encoder), so there are no binary fixtures in the repository. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 0xmrcl
