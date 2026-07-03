"""CLI entrypoint for claude-mux (argparse subcommands)."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to the requested subcommand."""
    parser = argparse.ArgumentParser(prog="claude-mux", description="tmux + git-worktree dashboard for Claude Code")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("dashboard", help="bootstrap the claude-mux session and enter its menu (default)")
    # Hidden: runs the Textual menu in-place as window 0's process (no bootstrap,
    # so it cannot recurse). Invoked by the session's window-0 command.
    sub.add_parser("_menu")
    sub.add_parser("popup", help="run the dashboard in popup mode (jump closes it)")
    sub.add_parser("install-hooks", help="install claude-mux hooks into ~/.claude/settings.json")
    sub.add_parser("uninstall-hooks", help="remove claude-mux hooks from ~/.claude/settings.json")

    hook_parser = sub.add_parser("hook", help="Claude Code hook entrypoint")
    hook_parser.add_argument("event", help="hook event name (SessionStart/SessionEnd/Notification/Stop)")

    args = parser.parse_args(argv)
    command = args.command or "dashboard"

    if command == "dashboard":
        # Bootstrap the owned session (window 0 = menu) and place the operator in
        # it: exec `tmux attach` when outside tmux, else switch-client. The menu
        # itself runs as window 0's process via the hidden `_menu` subcommand, so
        # this path does NOT run the Textual app directly (avoids recursion).
        from claude_mux import tmux

        tmux.bootstrap()
    elif command == "_menu":
        from claude_mux.app import run_dashboard
        from claude_mux.config import load_config

        run_dashboard(load_config(), popup=False)
    elif command == "popup":
        from claude_mux.app import run_dashboard
        from claude_mux.config import load_config

        run_dashboard(load_config(), popup=True)
    elif command == "install-hooks":
        from claude_mux.hooks import install_hooks

        for change in install_hooks():
            print(change)
    elif command == "uninstall-hooks":
        from claude_mux.hooks import uninstall_hooks

        for change in uninstall_hooks():
            print(change)
    elif command == "hook":
        from claude_mux.hooks import run_hook

        run_hook(args.event)
    else:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"unknown command: {command}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
