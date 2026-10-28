"""Shared types and the format registry."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, List, Optional


class FormatError(Exception):
    """The file looks like a supported format but could not be parsed."""


class UnsupportedFile(Exception):
    """The file type (or a variant of it) is not supported."""


@dataclass
class Options:
    strip_icc: bool = False
    keep_orientation: bool = False


@dataclass
class Item:
    """One metadata block (or notable structure) found in a file."""

    name: str
    size: int
    remove: bool
    note: str = ""
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name, "size": self.size, "remove": self.remove}
        if self.note:
            d["note"] = self.note
        if self.details:
            d["details"] = list(self.details)
        return d


def sha256(chunks: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _formats():
    from . import gif, jpeg, mp4, pdf, png, webp

    return [jpeg, png, webp, gif, pdf, mp4]


def detect(data: bytes):
    """Return the format module for ``data`` (by magic bytes) or None."""
    for mod in _formats():
        if mod.sniff(data):
            return mod
    return None


@dataclass
class Result:
    format: str
    items: List[Item]
    cleaned: Optional[bytes] = None

    @property
    def removed(self) -> List[Item]:
        return [i for i in self.items if i.remove]


def inspect_bytes(data: bytes, opts: Optional[Options] = None) -> Result:
    opts = opts or Options()
    mod = detect(data)
    if mod is None:
        raise UnsupportedFile("unsupported file type")
    return Result(mod.NAME, mod.scan(data, opts))


def wipe_bytes(data: bytes, opts: Optional[Options] = None) -> Result:
    opts = opts or Options()
    mod = detect(data)
    if mod is None:
        raise UnsupportedFile("unsupported file type")
    items = mod.scan(data, opts)
    cleaned = mod.clean(data, opts) if any(i.remove for i in items) else data
    return Result(mod.NAME, items, cleaned)


@dataclass
class Verification:
    ok: bool
    remaining: List[Item]
    warnings: List[Item]
    media_identical: Optional[bool]
    media_digest: Optional[str]


def verify_bytes(original: bytes, cleaned: bytes, opts: Optional[Options] = None) -> Verification:
    """Re-parse ``cleaned`` and check that nothing removable is left.

    Also compares a digest of the actual media payload (pixels / compressed
    image or video data) between original and cleaned output, where the
    format allows it.
    """
    opts = opts or Options()
    mod = detect(cleaned)
    if mod is None:
        return Verification(False, [Item("output is not a recognised file", len(cleaned), True)], [], None, None)
    try:
        items = mod.scan(cleaned, opts)
    except (FormatError, UnsupportedFile) as e:
        return Verification(False, [Item(f"output does not re-parse: {e}", len(cleaned), True)], [], None, None)
    remaining = [i for i in items if i.remove]
    warnings = [i for i in items if not i.remove and i.note.startswith("warning")]
    a = mod.media_digest(original)
    b = mod.media_digest(cleaned)
    identical = None if a is None or b is None else a == b
    ok = not remaining and identical is not False
    return Verification(ok, remaining, warnings, identical, b)
