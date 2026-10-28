"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from . import __version__
from .core import (FormatError, Item, Options, UnsupportedFile, detect, human_size,
                   inspect_bytes, verify_bytes, wipe_bytes)

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

EXIT_CODES = """exit codes:
  0  success (or, for inspect --check: no removable metadata found)
  1  --verify found metadata left in an output file (inspect --check: metadata found)
  2  usage error
  3  one or more files could not be processed (unsupported, damaged, I/O error)"""

FORMATS_HELP = "formats: JPEG, PNG, WebP, GIF, MP4/MOV, PDF (classic xref, best effort)"

GENERATED = re.compile(r"(\.clean(\.[^./\\]+)?|\.bak|\.metawipe-tmp)$")


def _wipe_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="metawipe",
        usage="metawipe [OPTIONS] PATH...\n       metawipe inspect [--json] [--check] FILE...",
        description="Strip metadata from photos, videos and PDFs, then prove it's gone.\n\n"
                    "By default the original is left untouched and a cleaned copy\n"
                    "NAME.clean.EXT is written next to it.",
        epilog=FORMATS_HELP + "\n\n" + EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="+", metavar="PATH", help="files or directories")
    p.add_argument("-r", "--recursive", action="store_true", help="process directories recursively")
    p.add_argument("-n", "--dry-run", action="store_true", help="only report what would be removed")
    p.add_argument("--verify", action="store_true",
                   help="re-read every output and exit 1 if known metadata remains")
    out = p.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", metavar="DIR", help="write cleaned copies into DIR")
    out.add_argument("--in-place", action="store_true", help="overwrite the original files")
    p.add_argument("--backup", action="store_true", help="with --in-place: keep the original as FILE.bak")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output files")
    p.add_argument("--strip-icc", action="store_true", help="also remove embedded ICC color profiles")
    p.add_argument("--keep-orientation", action="store_true",
                   help="JPEG: keep a minimal EXIF block with only the Orientation tag")
    p.add_argument("-q", "--quiet", action="store_true", help="only print errors and verification failures")
    p.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    p.add_argument("--version", action="version", version=f"metawipe {__version__}")
    return p


def _inspect_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="metawipe inspect",
        description="List the metadata blocks found in files (names and sizes only, never values).",
        epilog=EXIT_CODES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--json", action="store_true", help="print a JSON report")
    p.add_argument("--check", action="store_true", help="exit 1 if any removable metadata is found")
    return p


# ---------------------------------------------------------------- helpers

def _err(msg: str) -> None:
    print(f"metawipe: {msg}", file=sys.stderr)


def _clean_name(path: Path) -> Path:
    if path.suffix:
        return path.with_name(path.stem + ".clean" + path.suffix)
    return path.with_name(path.name + ".clean")


def _collect(paths: List[str], recursive: bool, outdir: Optional[Path]) -> Iterator[Tuple[Path, Optional[Path], bool]]:
    """Yield (file, root dir or None, explicitly named)."""
    out_resolved = outdir.resolve() if outdir else None
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            if not recursive:
                raise IsADirectoryError(f"{p}: is a directory (use -r)")
            for dirpath, dirnames, filenames in os.walk(p):
                dp = Path(dirpath)
                dirnames[:] = sorted(d for d in dirnames
                                     if out_resolved is None or (dp / d).resolve() != out_resolved)
                for name in sorted(filenames):
                    f = dp / name
                    if GENERATED.search(f.stem) or GENERATED.search(name) or f.is_symlink():
                        continue
                    yield f, p, False
        else:
            yield p, None, True


def _target(path: Path, root: Optional[Path], args) -> Path:
    if args.in_place:
        return path
    if args.output:
        rel = path.relative_to(root) if root else Path(path.name)
        return Path(args.output) / rel
    return _clean_name(path)


def _write(src: Path, dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".metawipe-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        shutil.copymode(src, tmp)
    except OSError:
        pass
    os.replace(tmp, dst)


def _orientation_warning(items: List[Item]) -> Optional[str]:
    for it in items:
        for d in it.details:
            if d.startswith("Orientation: ") and d != "Orientation: 1":
                return (f"EXIF {d} is dropped with the EXIF block; the image may display "
                        "rotated (use --keep-orientation for JPEG)")
    return None


# ---------------------------------------------------------------- wipe

def _process(path: Path, root: Optional[Path], explicit: bool, args, opts: Options) -> dict:
    rec: dict = {"path": str(path), "status": None}
    try:
        data = path.read_bytes()
        if detect(data) is None:
            if not explicit:
                rec["status"] = "skipped"
                return rec
            raise UnsupportedFile("unsupported file type (" + FORMATS_HELP + ")")
        res = wipe_bytes(data, opts)
    except (OSError, FormatError, UnsupportedFile) as e:
        rec.update(status="error", error=str(e) if not isinstance(e, OSError) else (e.strerror or str(e)))
        return rec
    except Exception as e:  # a parser bug must not abort a whole batch
        rec.update(status="error", error=f"internal error ({type(e).__name__}: {e}), please report this")
        return rec
    rec["format"] = res.format
    rec["removed"] = [i.to_dict() for i in res.items if i.remove]
    rec["kept"] = [i.to_dict() for i in res.items if not i.remove]
    warn = _orientation_warning(res.removed) if not opts.keep_orientation else None
    rec["warnings"] = [warn] if warn else []
    rec["warnings"] += [f"{i.name}: {i.note[len('warning: '):]}" for i in res.items
                        if not i.remove and i.note.startswith("warning")]
    has_meta = bool(res.removed)
    out = _target(path, root, args)
    rec["output"] = None
    try:
        if args.dry_run:
            rec["status"] = "would-clean" if has_meta else "clean"
            written = res.cleaned
        elif not has_meta and not args.output:
            rec["status"] = "clean"
            written = data
        else:
            if not args.in_place and out.exists() and not args.force:
                raise FileExistsError(f"{out}: output exists (use --force to overwrite)")
            if out.resolve() == path.resolve() and not args.in_place:
                raise FileExistsError(f"{out}: output would overwrite the input")
            if args.in_place and args.backup:
                bak = path.with_name(path.name + ".bak")
                if bak.exists() and not args.force:
                    raise FileExistsError(f"{bak}: backup exists (use --force to overwrite)")
                shutil.copy2(path, bak)
                rec["backup"] = str(bak)
            _write(path, out, res.cleaned)
            rec["status"] = "cleaned" if has_meta else "copied"
            rec["output"] = str(out)
            written = out.read_bytes()  # verify what is actually on disk
    except OSError as e:
        rec.update(status="error", error=str(e))
        return rec
    if args.verify:
        v = verify_bytes(data, written, opts)
        rec["verify"] = {
            "ok": v.ok,
            "remaining": [i.to_dict() for i in v.remaining],
            "media_identical": v.media_identical,
            "media_sha256": v.media_digest,
        }
    return rec


def _print_items(prefix: str, items: List[dict]) -> None:
    for it in items:
        line = f"  {prefix:<8} {it['name']} ({human_size(it['size'])})"
        if it.get("note"):
            line += f"  [{it['note']}]"
        print(line)
        if it.get("details"):
            print(" " * 11 + " | ".join(it["details"]))


def _print_record(rec: dict, args) -> None:
    status = rec["status"]
    v = rec.get("verify")
    failed = v is not None and not v["ok"]
    if status == "skipped":
        return
    if status == "error":
        _err(f"{rec['path']}: {rec['error']}")
        return
    if args.quiet and not failed:
        return
    head = f"{rec['path']}  [{rec['format']}]"
    if status == "would-clean":
        head += "  (dry run)"
    elif rec.get("output") and rec["output"] != rec["path"]:
        head += f"  ->  {rec['output']}"
    elif status == "cleaned":
        head += "  (in place)"
    print(head)
    if not args.quiet:
        if rec["removed"]:
            _print_items("would rm" if status == "would-clean" else "removed", rec["removed"])
        else:
            print("  nothing to remove")
        _print_items("kept", rec["kept"])
        for w in rec["warnings"]:
            print(f"  warning  {w}")
    if v is not None:
        if v["ok"]:
            media = {True: "media data identical", None: "media digest n/a for this format"}[v["media_identical"]]
            print(f"  verify   OK: no known metadata left, {media}")
        else:
            print("  verify   FAILED")
            for it in v["remaining"]:
                print(f"           still present: {it['name']} ({human_size(it['size'])})")
            if v["media_identical"] is False:
                print("           media data changed")


def _wipe_main(argv: List[str]) -> int:
    parser = _wipe_parser()
    args = parser.parse_args(argv)
    if args.backup and not args.in_place:
        parser.error("--backup only makes sense with --in-place")
    opts = Options(strip_icc=args.strip_icc, keep_orientation=args.keep_orientation)
    outdir = Path(args.output) if args.output else None
    records = []
    try:
        for f, root, explicit in _collect(args.paths, args.recursive, outdir):
            if not f.exists():
                records.append({"path": str(f), "status": "error", "error": "no such file"})
            else:
                records.append(_process(f, root, explicit, args, opts))
            if not args.json:
                _print_record(records[-1], args)
    except IsADirectoryError as e:
        _err(str(e))
        return EXIT_USAGE
    summary = {k: sum(1 for r in records if r["status"] == k)
               for k in ("cleaned", "would-clean", "copied", "clean", "skipped", "error")}
    verified = [r for r in records if "verify" in r]
    summary["verify_passed"] = sum(1 for r in verified if r["verify"]["ok"])
    summary["verify_failed"] = len(verified) - summary["verify_passed"]
    if args.json:
        print(json.dumps({"metawipe": __version__, "dry_run": args.dry_run, "files": records,
                          "summary": summary}, indent=2))
    elif not args.quiet:
        labels = {"cleaned": "cleaned", "would-clean": "would be cleaned", "copied": "copied",
                  "clean": "already clean", "skipped": "skipped (unsupported)", "error": "errors"}
        if summary["error"] == 1:
            labels["error"] = "error"
        parts = [f"{summary[k]} {label}" for k, label in labels.items()
                 if summary[k] or k in ("cleaned", "error")]
        line = "summary: " + ", ".join(parts)
        if verified:
            line += f"; verify: {summary['verify_passed']} passed, {summary['verify_failed']} failed"
        print(line)
    if summary["verify_failed"]:
        return EXIT_VERIFY_FAILED
    if summary["error"]:
        return EXIT_ERROR
    return EXIT_OK


# ---------------------------------------------------------------- inspect

def _inspect_main(argv: List[str]) -> int:
    args = _inspect_parser().parse_args(argv)
    results, errors, found = [], 0, 0
    for name in args.files:
        path = Path(name)
        try:
            data = path.read_bytes()
            res = inspect_bytes(data)
        except (OSError, FormatError, UnsupportedFile) as e:
            errors += 1
            msg = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
            _err(f"{path}: {msg}")
            results.append({"path": str(path), "error": msg})
            continue
        removable = [i for i in res.items if i.remove]
        found += bool(removable)
        rec = {"path": str(path), "format": res.format, "size": len(data),
               "metadata": [i.to_dict() for i in res.items]}
        results.append(rec)
        if args.json:
            continue
        print(f"{path}  [{res.format}, {human_size(len(data))}]")
        if not res.items:
            print("  no metadata found")
        for it in res.items:
            line = f"  {'remove' if it.remove else 'keep':<7} {it.name} ({human_size(it.size)})"
            if it.note:
                line += f"  [{it.note}]"
            print(line)
            if it.details:
                print(" " * 10 + " | ".join(it.details))
    if args.json:
        print(json.dumps({"metawipe": __version__, "files": results}, indent=2))
    if errors:
        return EXIT_ERROR
    if args.check and found:
        return EXIT_VERIFY_FAILED
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "inspect":
        return _inspect_main(argv[1:])
    return _wipe_main(argv)


if __name__ == "__main__":
    sys.exit(main())
