import argparse
import atexit
import os
import shutil
import signal
import sys
from pathlib import Path

# Allow running this file directly from a copied runtime directory like
# `python .carta/carta/cli.py scan`.
# When executed as a script, `sys.path[0]` becomes the script directory
# (e.g. `.../.carta/carta`), but importing the `carta` package requires
# its parent directory (e.g. `.../.carta`) to be on `sys.path`.
if __name__ == "__main__" and __package__ is None:
    package_parent = Path(__file__).resolve().parent.parent
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))

from carta import __version__
from carta.config import find_config


def _notify_if_update(cfg_path=None, cfg=None):
    """Call maybe_notify if we have a config context. Silently skips on error."""
    try:
        from carta.update.checker import maybe_notify
        carta_dir = cfg_path.parent if cfg_path else None
        maybe_notify(carta_dir, cfg or {})
    except Exception:
        pass


def _embed_lock_read_pid(lock_path: Path):
    try:
        return int(lock_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _embed_lock_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    else:
        return True


def _acquire_embed_lock(lock_path: Path) -> None:
    """Ensure only one live embed process; create lock atomically; remove stale locks."""
    while True:
        if not lock_path.exists():
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                return
            except FileExistsError:
                continue

        existing_pid = _embed_lock_read_pid(lock_path)
        if existing_pid is None or existing_pid <= 0:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        if _embed_lock_pid_alive(existing_pid):
            print(
                f"carta embed is already running (PID: {existing_pid}). "
                "Wait for it to finish or remove .carta/embed.lock if it is stale.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def cmd_scan(args):
    from carta.config import load_config
    from carta.scanner.scanner import run_scan
    from carta.ui import Progress
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_audit"):
        print("doc_audit module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    output_path = cfg_path.parent / "scan-results.json"
    with Progress() as progress:
        results = run_scan(
            cfg_path.parent.parent, cfg,
            output_path=output_path,
            verbose=False,
            progress=progress,
        )
    issue_count = len(results["issues"])
    print(f"Results at {output_path}")
    # Print related: auto-suggestions (only when Qdrant is available)
    suggestions = results.get("related_suggestions") or []
    if suggestions:
        print()
        print("\U0001f4ce Suggested related: links (similarity ≥ 0.85):")
        for s in suggestions:
            print(f"  {s['doc']}: {s['suggested']} ({s['score']:.2f})")
    _notify_if_update(cfg_path, cfg)

def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed, discover_pending_files, run_embed_file
    from carta.ui import Progress
    import time

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_embed"):
        print("doc_embed module is disabled in config.", file=sys.stderr)
        sys.exit(1)

    # Targeted embed: one or more specific files, no lock, no discovery scan.
    if getattr(args, "files", None):
        files = args.files
        embedded = 0
        errors = []

        with Progress(total=len(files)) as progress:
            for idx, file_arg in enumerate(files, start=1):
                file_path = Path(file_arg)
                progress.file(idx, file_path.name)
                t0 = time.monotonic()
                try:
                    result = run_embed_file(file_path, cfg, force=True, progress=progress)
                    elapsed = time.monotonic() - t0
                    progress.done(chunks=result.get("chunks", 0), elapsed=elapsed)
                    embedded += 1
                except FileNotFoundError as e:
                    progress.error(str(e))
                    errors.append(str(e))
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    progress.error(str(e))
                    errors.append(f"{file_path.name}: {e}")

        progress.summary(embedded=embedded, skipped=0, errors=len(errors))
        _notify_if_update(cfg_path, cfg)
        sys.exit(1 if errors else 0)

    # FT-5: Concurrency lock — only one embed process at a time (atomic create + stale PID).
    lock_path = cfg_path.parent / "embed.lock"
    _acquire_embed_lock(lock_path)

    def _remove_lock():
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_remove_lock)

    def _signal_handler(signum, frame):
        _remove_lock()
        sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _signal_handler)

    # Discover pending count upfront so Progress knows the total.
    # run_embed will also call discover_pending_files internally — that's fine,
    # it's a cheap filesystem scan.
    repo_root = cfg_path.parent.parent
    pending = discover_pending_files(repo_root)

    with Progress(total=len(pending)) as progress:
        summary = run_embed(repo_root, cfg, verbose=False, progress=progress)
    progress.summary(
        embedded=summary["embedded"],
        skipped=summary["skipped"],
        errors=len(summary["errors"]),
    )
    _notify_if_update(cfg_path, cfg)
    if summary["errors"]:
        sys.exit(1)

def cmd_search(args):
    from carta.config import load_config
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_search
    query = " ".join(args.query)
    try:
        results = run_search(query, cfg, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not results:
        print(
            "No results found. If nothing is embedded yet, run `carta embed` first; "
            "otherwise try different wording."
        )
        _notify_if_update(cfg_path, cfg)
        return
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")

    _notify_if_update(cfg_path, cfg)

def cmd_update(args):
    """Check for and apply carta updates."""
    from carta.update.updater import run_update, print_check
    if args.check:
        print_check()
        sys.exit(0)
    code = run_update(yes=args.yes)
    sys.exit(code)


def _platformio_carta_paths_on_path() -> list[Path]:
    found: list[Path] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        candidate = Path(d) / "carta"
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                rp = candidate.resolve()
                if "platformio" in str(rp).lower():
                    found.append(rp)
        except OSError:
            continue
    return found


def _check_path_conflict() -> None:
    """Warn when a different 'carta' binary is earlier on PATH than the one we're running."""
    carta_on_path = shutil.which("carta")
    if carta_on_path is None:
        return
    # Resolve symlinks so we compare real paths
    running = Path(sys.executable).resolve()
    on_path = Path(carta_on_path).resolve()
    # If the carta binary on PATH lives inside the same prefix as our Python interpreter,
    # there is no conflict.
    try:
        on_path.relative_to(running.parent.parent)
        pio = _platformio_carta_paths_on_path()
        if pio and "platformio" not in str(carta_on_path).lower():
            print(
                f"Note: a PlatformIO `carta` also exists on PATH ({pio[0]}). "
                "If the wrong tool runs, put pipx/venv first, e.g.: "
                'export PATH="$HOME/.local/bin:$PATH"'
            )
        return  # same prefix — no conflict
    except ValueError:
        pass
    # A different binary is shadowing ours.
    print(f"Warning: 'carta' found on PATH at {carta_on_path} does not match the running interpreter.")
    if ".platformio" in carta_on_path:
        print("  This appears to be PlatformIO's carta binary, which shadows carta-cc.")
    print("  To fix: add the following line to your ~/.zshrc or ~/.bashrc, then restart your terminal:")
    print('    export PATH="$HOME/.local/bin:$PATH"')
    print("  Then verify with: which carta")


def cmd_init(args):
    _check_path_conflict()
    from carta.install.bootstrap import run_bootstrap
    run_bootstrap(Path.cwd(), skip_skills=getattr(args, "skip_skills", False))
    _notify_if_update()

def cmd_doctor(args):
    """Run diagnostic checks and optionally auto-fix issues."""
    from carta.install.preflight import PreflightChecker, PreflightResult
    from carta.install.auto_fix import AutoInstaller
    interactive = not (args.yes or args.fix)  # --yes or --fix disables prompts
    checker = PreflightChecker(interactive=interactive, verbose=args.verbose, project_root=Path.cwd())
    result = checker.run()

    # Print report
    if args.json:
        print(result.to_json())
    else:
        result.print_report(verbose=args.verbose)

    # Offer to fix fixable failures (always interactive, --fix just auto-confirms)
    if result.fixable_failures:
        if not args.json:
            print(f"\n🔧 Attempting to fix {len(result.fixable_failures)} issue(s)...")
        installer = AutoInstaller(interactive=interactive, verbose=args.verbose)
        fixes = installer.fix_all(result)

        successful = sum(1 for success in fixes.values() if success)
        if not args.json:
            print(f"\n✅ Fixed: {successful}/{len(fixes)}")

        # Re-run checks to verify fixes
        if successful > 0 and not args.json:
            print("\n🔄 Re-running checks to verify fixes...")
            result = checker.run()
            result.print_report(verbose=args.verbose)
    elif args.fix and not args.json:
        print("\n✅ No fixable issues found.")

    # Exit with error code if critical failures remain
    if not result.can_proceed():
        if not args.json:
            installer = AutoInstaller(interactive=False)
            installer.print_setup_guide(result)
        _notify_if_update()
        sys.exit(1)

    _notify_if_update()
    sys.exit(0)

def cmd_audit(args):
    """Run audit to detect inconsistencies in the embed pipeline.

    Usage:
        carta audit [--output REPORT.json]

    Detects orphaned chunks, missing sidecars, stale files, and more.
    Reports to JSON for agent-assisted repair or manual review.
    """
    from carta.audit.audit import run_audit
    from carta.config import load_config
    import json

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent

    output_path = args.output if hasattr(args, 'output') and args.output else "audit-report.json"

    try:
        result = run_audit(cfg, repo_root, verbose=True)

        # Write report to JSON
        output_file = repo_root / output_path
        output_file.write_text(json.dumps(result, indent=2))

        # Print summary
        summary = result["summary"]
        print(f"\nAudit complete: {summary['total_issues']} issues found")
        if summary["total_issues"] > 0:
            for cat, count in summary["by_category"].items():
                print(f"  {cat}: {count}")

        print(f"Report saved to: {output_path}")

        sys.exit(0)

    except Exception as e:
        print(f"Error: Audit failed: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(prog="carta")
    parser.add_argument("--version", action="version", version=f"carta {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Initialize Carta in the current project")
    init_p.add_argument(
        "--skip-skills",
        action="store_true",
        help="Do not install Carta skills to ~/.claude/skills or .claude/skills",
    )
    sub.add_parser("scan")
    embed_p = sub.add_parser("embed")
    embed_p.add_argument(
        "files",
        nargs="*",
        help="Specific file(s) to embed immediately (skips full pipeline and lock)",
    )

    audit_p = sub.add_parser(
        "audit",
        help="Detect inconsistencies in embed pipeline and write JSON report"
    )
    audit_p.add_argument(
        "--output",
        default="audit-report.json",
        help="Output path for JSON report (default: audit-report.json)"
    )
    audit_p.set_defaults(func=cmd_audit)

    # Doctor command with options
    doctor_p = sub.add_parser("doctor", help="Diagnose Carta installation and environment")
    doctor_p.add_argument("--fix", action="store_true", help="Attempt to auto-fix issues")
    doctor_p.add_argument("--yes", "-y", action="store_true", help="Auto-confirm fixes without prompting")
    doctor_p.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    doctor_p.add_argument("--json", action="store_true", help="Output in JSON format")
    
    search_p = sub.add_parser("search", help="Semantic search over embedded documents")
    search_p.add_argument("query", nargs="+")
    update_p = sub.add_parser("update", help="Update carta to the latest version")
    update_p.add_argument("--check", action="store_true", help="Show available version without upgrading")
    update_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    dispatch = {
        "init": cmd_init,
        "scan": cmd_scan,
        "embed": cmd_embed,
        "search": cmd_search,
        "audit": cmd_audit,
        "doctor": cmd_doctor,
        "update": cmd_update,
    }

    if args.command not in dispatch:
        parser.print_help()
        sys.exit(1)

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        sys.exit(130)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        label = type(e).__name__
        print(f"Error ({label}): {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
