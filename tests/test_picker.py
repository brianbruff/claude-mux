"""Unit tests for claude_mux.picker that DO NOT launch yazi.

subprocess.run is monkeypatched to emulate yazi writing the chosen directory to
the ``--cwd-file`` temp path; app.suspend() is faked with a trivial context
manager. The safety-critical invariant under test: a picker failure yields None,
never an exception that would crash the TUI.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from claude_mux import picker


class FakeApp:
    """Stands in for a Textual App: app.suspend() must be a context manager."""

    def __init__(self):
        self.suspended = False

    @contextlib.contextmanager
    def suspend(self):
        self.suspended = True
        try:
            yield
        finally:
            self.suspended = False


def test_yazi_available_true(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")
    assert picker.yazi_available() is True


def test_yazi_available_false(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", lambda name: None)
    assert picker.yazi_available() is False


def test_pick_directory_returns_chosen_path(monkeypatch, tmp_path):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")
    chosen = tmp_path / "selected-dir"

    def fake_run(cmd, *args, **kwargs):
        # Emulate yazi: extract the --cwd-file path and write the chosen dir into it.
        cwd_file = next(a.split("=", 1)[1] for a in cmd if a.startswith("--cwd-file="))
        Path(cwd_file).write_text(str(chosen), encoding="utf-8")

    monkeypatch.setattr(picker.subprocess, "run", fake_run)

    app = FakeApp()
    result = picker.pick_directory(app)

    assert result == chosen
    assert app.suspended is False  # restored cleanly


def test_pick_directory_passes_start_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")
    start = tmp_path / "start-here"
    seen_cmd = {}

    def fake_run(cmd, *args, **kwargs):
        seen_cmd["cmd"] = list(cmd)
        cwd_file = next(a.split("=", 1)[1] for a in cmd if a.startswith("--cwd-file="))
        Path(cwd_file).write_text(str(tmp_path / "picked"), encoding="utf-8")

    monkeypatch.setattr(picker.subprocess, "run", fake_run)

    picker.pick_directory(FakeApp(), start=start)
    assert str(start) in seen_cmd["cmd"]


def test_pick_directory_yazi_missing_returns_none(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", lambda name: None)

    def boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when yazi is missing")

    monkeypatch.setattr(picker.subprocess, "run", boom)
    assert picker.pick_directory(FakeApp()) is None


def test_pick_directory_empty_temp_returns_none(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")

    def fake_run(cmd, *args, **kwargs):
        # yazi cancelled: leave the cwd-file empty.
        pass

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    assert picker.pick_directory(FakeApp()) is None


def test_pick_directory_swallows_errors(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")

    def boom(*args, **kwargs):
        raise RuntimeError("yazi blew up")

    monkeypatch.setattr(picker.subprocess, "run", boom)
    # Must not raise; a picker failure never crashes the TUI.
    assert picker.pick_directory(FakeApp()) is None


def test_pick_directory_cleans_up_temp(monkeypatch, tmp_path):
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/yazi")
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        cwd_file = next(a.split("=", 1)[1] for a in cmd if a.startswith("--cwd-file="))
        captured["cwd_file"] = cwd_file
        Path(cwd_file).write_text(str(tmp_path), encoding="utf-8")

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    picker.pick_directory(FakeApp())
    assert not Path(captured["cwd_file"]).exists()  # temp removed in finally
