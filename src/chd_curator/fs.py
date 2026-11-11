"""Writes that land whole. Copied from the atlas's ``chd_atlas/fs.py`` at b4c8191
(``write_bytes_atomically`` only), with its comments."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_bytes_atomically(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in a single step.

    Opening the destination for writing truncates it before the payload lands, so
    a write that failed part-way left a file that is neither its old content nor
    its new one. For a curated literature file that is lost work, which committed
    history cannot restore if it was never committed.

    Writing a sibling temporary and renaming it leaves the target as either its
    old content or the complete new content, never a prefix of the new one.

    ``path.parent`` must exist. `mkstemp` would otherwise raise
    `FileNotFoundError` naming the temporary it was about to create, a path the
    caller never chose and cannot correlate with anything they typed.

    A failed write raises rather than being folded into a report: there is no
    report for it to appear in, and a lost file must not read as a clean exit.
    """
    # A sibling so `os.replace` stays within one filesystem, where it is atomic.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        # Binary, and the payload is bytes, so the caller's newlines reach the
        # disk untouched. `open(..., "w", encoding="utf-8")` defaults to
        # `newline=None`, which rewrites every "\n" to `os.linesep`.
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        # `mkstemp` creates at 0600, which would leave a committed, world-readable
        # artifact owner-only on whichever machine wrote it. Set the mode
        # explicitly so it does not depend on how the file happened to be made.
        #
        # Before the replace, not after: afterwards there is a window in which
        # the file is visible at its published name with the wrong mode, and the
        # chmod would apply to whatever now sits at `path` rather than to the
        # bytes just written.
        os.chmod(temporary, 0o644)
        # No fsync: this guards against a failed or interrupted write, not
        # against power loss.
        os.replace(temporary, path)
    # BaseException, not Exception: a Ctrl-C between mkstemp and replace would
    # otherwise strand a `.tmp` next to the curated file.
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
