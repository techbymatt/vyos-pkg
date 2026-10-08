"""Stage audited upstream adaptations and fail on drift before writing files."""

from __future__ import annotations

from pathlib import Path

try:
    from .workflow_utils import atomic_write
except ImportError:
    from workflow_utils import atomic_write


class CheckedEdits:
    """A set of exact replacements, validated before any file is changed."""

    def __init__(self) -> None:
        """Start an empty adaptation transaction."""
        self.contents: dict[Path, str] = {}

    def read(self, path: Path) -> str:
        """Read the staged contents, following builder symlinks once."""
        path = path.resolve()
        if path not in self.contents:
            self.contents[path] = path.read_text(encoding="utf-8")
        return self.contents[path]

    def set(self, path: Path, text: str) -> None:
        """Stage already-validated text, without changing the upstream file yet."""
        self.contents[path.resolve()] = text

    def replace(self, path: Path, old: str, new: str, *, count: int = 1) -> None:
        """Require exactly count audited commands before staging a replacement."""
        text = self.read(path)
        if text.count(old) != count:
            expected = "exactly one" if count == 1 else str(count)
            raise ValueError(f"{path}: expected {expected} audited command {old!r}")
        self.set(path, text.replace(old, new))

    def commit(self) -> None:
        """Write the fully validated edits, preserving executable bits/symlinks."""
        for path, text in self.contents.items():
            atomic_write(path, text.encode("utf-8"))


def replace_once(path: Path, old: str, new: str) -> None:
    """Apply a single checked replacement; use CheckedEdits for multi-file changes."""
    edits = CheckedEdits()
    edits.replace(path, old, new)
    edits.commit()
