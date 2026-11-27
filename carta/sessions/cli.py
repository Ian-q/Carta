"""`carta sessions ...` — the session-memory commands.

The parser lives here rather than in ``carta/cli.py`` so session subcommands can be
added without touching the main dispatcher.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    """Register ``sessions`` on the top-level subparsers."""
    p = sub.add_parser(
        "sessions", help="Session memory: index Claude Code transcripts for recall")
    ssub = p.add_subparsers(dest="sessions_action")
    ip = ssub.add_parser(
        "index", help="Index new or changed transcripts of this project into "
                      "<project>_sessions")
    ip.add_argument("--backfill", action="store_true",
                    help="Consider every transcript, not only those modified within "
                         "sessions.window_days")
    ip.add_argument("--transcripts-dir", metavar="DIR", default=None,
                    help="Where transcripts live (default: sessions.transcripts_dir, "
                         "~/.claude/projects)")
    ip.add_argument("--session", metavar="FILE", default=None,
                    help="Index just this transcript file")
    ip.add_argument("--force", action="store_true",
                    help="Re-index even transcripts that have not changed")
    ip.add_argument("--dry-run", action="store_true",
                    help="Build the records and list them; write nothing")
    ip.add_argument("--json", action="store_true", help="Print the summary as JSON")
    p.set_defaults(sessions_usage=p.format_usage())


def run(args) -> None:
    """Dispatch ``carta sessions <action>``."""
    if getattr(args, "sessions_action", None) != "index":
        print(getattr(args, "sessions_usage", "usage: carta sessions index"), end="",
              file=sys.stderr)
        sys.exit(1)
    _cmd_index(args)


def _cmd_index(args) -> None:
    from carta.config import find_config, load_config
    from carta.embed.lock import EmbedLockHeld, embed_lock
    from carta.sessions.index import SessionIndexError, run_index, state_path

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent
    session_file = Path(args.session).expanduser() if args.session else None
    if session_file is not None and not session_file.is_file():
        print(f"Error: transcript not found: {session_file}", file=sys.stderr)
        sys.exit(1)

    lock = state_path(cfg).parent / "index.lock"
    try:
        with embed_lock(lock):
            summary = run_index(
                cfg, repo_root,
                transcripts_dir=Path(args.transcripts_dir) if args.transcripts_dir else None,
                session_file=session_file,
                backfill=args.backfill,
                force=args.force,
                dry_run=args.dry_run,
                verbose=not args.json,
            )
    except EmbedLockHeld as e:
        print(f"Error: another `carta sessions index` is running (PID {e.pid}).",
              file=sys.stderr)
        sys.exit(1)
    except SessionIndexError as e:
        print(f"Error: {e}. Transcripts not yet indexed are retried on the next run.",
              file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(summary, indent=2))
