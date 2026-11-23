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

    bp = ssub.add_parser(
        "brief", help="Recent sessions of this worktree, then this repo (at most 15 lines; "
                      "no model call; prints nothing when Qdrant is unreachable)")
    bp.add_argument("--worktree-limit", type=int, default=None, metavar="N",
                    help="Sessions shown for this worktree (default: sessions.brief.worktree)")
    bp.add_argument("--repo-limit", type=int, default=None, metavar="N",
                    help="Sessions shown for the rest of the repo (default: sessions.brief.repo)")

    hp = ssub.add_parser(
        "hook", help="Claude Code hook entry point (reads the hook's JSON on stdin; "
                     "always exits 0 and is silent on errors)")
    hp.add_argument("event", choices=["stop", "session-start"],
                    help="stop: index the ended session in the background (Stop, "
                         "SessionEnd); session-start: print the brief")
    p.set_defaults(sessions_usage=p.format_usage())


def add_recall_parser(sub) -> None:
    """Register the top-level ``recall`` command."""
    rp = sub.add_parser(
        "recall", help="Search past Claude Code sessions of this project")
    rp.add_argument("query", help="What you remember about the session, in your own words")
    rp.add_argument("--since", default=None, metavar="SPAN",
                    help="Only sessions started within SPAN (14d, 36h, 2w) or since a date "
                         "(YYYY-MM-DD)")
    rp.add_argument("--worktree", default=None, metavar="NAME",
                    help="Only sessions run in this worktree ('.' = the current one)")
    rp.add_argument("--branch", default=None, metavar="NAME",
                    help="Only sessions on this git branch")
    rp.add_argument("--limit", type=int, default=5, metavar="N",
                    help="Sessions to return (default 5)")
    rp.add_argument("--json", action="store_true", help="Print the results as JSON")


def run(args) -> None:
    """Dispatch ``carta sessions <action>``."""
    action = getattr(args, "sessions_action", None)
    if action == "index":
        _cmd_index(args)
    elif action == "brief":
        _cmd_brief(args)
    elif action == "hook":
        from carta.sessions.hooks import run_hook
        sys.exit(run_hook(args.event))
    else:
        print(getattr(args, "sessions_usage", "usage: carta sessions index"), end="",
              file=sys.stderr)
        sys.exit(1)


def _cmd_brief(args) -> None:
    """Print the primer. Silent, exit 0, on every failure: it runs at session start."""
    from carta.sessions.brief import safe_brief
    lines = safe_brief(None, per_worktree=args.worktree_limit, per_repo=args.repo_limit)
    if lines:
        print("\n".join(lines))


def current_worktree(cfg_path: Path, cwd: Path | None = None) -> str | None:
    """Name of the worktree ``cwd`` is in, as the index records it."""
    from carta.sessions.project import git_worktrees, locate
    repo_root = cfg_path.parent.parent
    loc = locate(str(cwd or Path.cwd()), repo_root, git_worktrees(repo_root))
    return loc.worktree if loc else None


def run_recall(args) -> None:
    """``carta recall``."""
    from carta.config import find_config, load_config
    from carta.sessions.recall import RecallUnavailable, format_results, parse_since, recall

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not (cfg.get("modules") or {}).get("session_memory", True):
        print("Session memory is off (modules.session_memory: false).", file=sys.stderr)
        sys.exit(1)
    try:
        since_ts = parse_since(args.since)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    worktree = args.worktree
    if worktree == ".":
        worktree = current_worktree(cfg_path)
    try:
        results = recall(args.query, cfg, since_ts=since_ts, worktree=worktree,
                         branch=args.branch, limit=args.limit)
    except RecallUnavailable as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(results, indent=2))
    elif results:
        print(format_results(results), end="")
    else:
        print("No matching sessions.")


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
