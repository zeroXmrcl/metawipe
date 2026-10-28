import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from metawipe import __version__, cli, jpeg
from metawipe.core import inspect_bytes

from . import samples as S


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as e:  # argparse
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.jpg = self.dir / "photo.jpg"
        self.jpg.write_bytes(S.jpeg_with_metadata())
        self.original = self.jpg.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def has_meta(self, path: Path) -> bool:
        return any(i.remove for i in inspect_bytes(path.read_bytes()).items)

    def test_default_is_non_destructive(self):
        code, out, _ = run(str(self.jpg))
        self.assertEqual(code, 0)
        self.assertEqual(self.jpg.read_bytes(), self.original)
        clean = self.dir / "photo.clean.jpg"
        self.assertTrue(clean.exists())
        self.assertFalse(self.has_meta(clean))
        self.assertIn("removed  APP1 EXIF", out)

    def test_existing_output_needs_force(self):
        run(str(self.jpg))
        code, _, err = run(str(self.jpg))
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("--force", err)
        self.assertEqual(run("-f", str(self.jpg))[0], 0)

    def test_dry_run_writes_nothing(self):
        code, out, _ = run("-n", "--verify", str(self.jpg))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])
        self.assertIn("would rm", out)

    def test_in_place_with_backup(self):
        code, _, _ = run("--in-place", "--backup", "--verify", str(self.jpg))
        self.assertEqual(code, 0)
        self.assertFalse(self.has_meta(self.jpg))
        self.assertEqual((self.dir / "photo.jpg.bak").read_bytes(), self.original)

    def test_backup_requires_in_place(self):
        self.assertEqual(run("--backup", str(self.jpg))[0], cli.EXIT_USAGE)

    def test_verify_failure_exit_code(self):
        with mock.patch.object(jpeg, "clean", lambda data, opts: data + b""):
            code, out, _ = run("--verify", "-f", str(self.jpg))
        self.assertEqual(code, cli.EXIT_VERIFY_FAILED)
        self.assertIn("verify   FAILED", out)

    def test_recursive_output_dir(self):
        sub = self.dir / "in" / "nested"
        sub.mkdir(parents=True)
        (sub / "a.png").write_bytes(S.png_with_metadata())
        (self.dir / "in" / "b.gif").write_bytes(S.gif_with_metadata())
        (self.dir / "in" / "c.txt").write_text("not media")
        (self.dir / "in" / "old.clean.png").write_bytes(S.png_with_metadata())
        outdir = self.dir / "out"
        code, out, _ = run("-r", "--verify", "-o", str(outdir), str(self.dir / "in"))
        self.assertEqual(code, 0, out)
        self.assertTrue((outdir / "nested" / "a.png").exists())
        self.assertTrue((outdir / "b.gif").exists())
        self.assertFalse((outdir / "c.txt").exists())
        self.assertFalse((outdir / "old.clean.png").exists())  # generated files are skipped
        self.assertFalse(self.has_meta(outdir / "nested" / "a.png"))
        self.assertIn("1 skipped", out)

    def test_directory_without_recursive(self):
        self.assertEqual(run(str(self.dir))[0], cli.EXIT_USAGE)

    def test_unsupported_explicit_file(self):
        txt = self.dir / "notes.txt"
        txt.write_text("hello")
        code, _, err = run(str(txt))
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("unsupported", err)

    def test_missing_file(self):
        self.assertEqual(run(str(self.dir / "nope.jpg"))[0], cli.EXIT_ERROR)

    def test_json_report(self):
        code, out, _ = run("--json", "--verify", str(self.jpg))
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(rep["metawipe"], __version__)
        f = rep["files"][0]
        self.assertEqual(f["status"], "cleaned")
        self.assertTrue(f["verify"]["ok"])
        self.assertTrue(f["verify"]["media_identical"])
        self.assertIn("APP1 EXIF", [r["name"] for r in f["removed"]])

    def test_quiet(self):
        code, out, _ = run("-q", "--verify", str(self.jpg))
        self.assertEqual((code, out), (0, ""))

    def test_already_clean_writes_nothing(self):
        plain = self.dir / "plain.jpg"
        plain.write_bytes(S.jpeg_plain())
        code, out, _ = run(str(plain))
        self.assertEqual(code, 0)
        self.assertFalse((self.dir / "plain.clean.jpg").exists())
        self.assertIn("nothing to remove", out)

    def test_inspect(self):
        code, out, _ = run("inspect", str(self.jpg))
        self.assertEqual(code, 0)
        self.assertIn("GPS: yes", out)
        self.assertNotIn("ExampleCam", out)
        self.assertEqual(run("inspect", "--check", str(self.jpg))[0], cli.EXIT_VERIFY_FAILED)
        run(str(self.jpg))
        self.assertEqual(run("inspect", "--check", str(self.dir / "photo.clean.jpg"))[0], 0)
        rep = json.loads(run("inspect", "--json", str(self.jpg))[1])
        self.assertEqual(rep["files"][0]["format"], "JPEG")

    def test_version(self):
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX-only")
    def test_mode_is_preserved(self):
        os.chmod(self.jpg, 0o640)
        run(str(self.jpg))
        self.assertEqual(os.stat(self.dir / "photo.clean.jpg").st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
