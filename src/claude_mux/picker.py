"""Directory picker via yazi, run in the restored terminal while the TUI suspends.

A picker failure must NEVER crash the TUI: every path out of ``pick_directory``
either returns a ``Path`` or ``None`` (cancel / yazi missing / empty temp / any
error), and the temp file is always cleaned up.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def yazi_available() -> bool:
    """True if the ``yazi`` binary is on PATH."""
    return shutil.which("yazi") is not None


def pick_directory(app, start: Path | None = None) -> Path | None:
    """Suspend the Textual ``app`` and let the operator pick a directory in yazi.

    Runs ``yazi --cwd-file=<tmp> [start]`` in the restored terminal; yazi writes
    the directory it was in on exit to ``<tmp>``. Returns that directory as a
    ``Path``, or ``None`` on cancel / yazi missing / empty temp / any error.
    No exception escapes.
    """
    if not yazi_available():
        return None

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="claude-mux-picker-", suffix=".cwd")
        os.close(fd)

        cmd = ["yazi", f"--cwd-file={tmp_path}"]
        if start is not None:
            cmd.append(str(start))

        with app.suspend():
            subprocess.run(cmd)

        chosen = Path(tmp_path).read_text(encoding="utf-8").strip()
        if not chosen:
            return None
        return Path(chosen)
    except Exception:
        # A picker failure must never crash the TUI.
        return None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
