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


def find_config(start: Path = None) -> Path:
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / ".carta" / "config.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise FileNotFoundError(
        ".carta/config.yaml not found (searched up to filesystem root). "
        "Run `carta init` first."
    )

def cmd_scan(args):
    from carta.config import load_config
    from carta.scanner.scanner import run_scan
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_audit"):
        print("doc_audit module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    output_path = cfg_path.parent / "scan-results.json"
    results = run_scan(cfg_path.parent.parent, cfg, output_path=output_path)
    issue_count = len(results["issues"])
    print(f"Scan complete: {issue_count} issue(s). Results at {output_path}")

def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_embed"):
        print("doc_embed module is disabled in config.", file=sys.stderr)
        sys.exit(1)

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

    summary = run_embed(Path.cwd(), cfg)
    print(f"Embedded: {summary['embedded']}, Skipped: {summary['skipped']}")
    if summary["errors"]:
        sys.exit(1)

def cmd_search(args):
    from carta.config import load_config
    cfg = load_config(find_config())
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_search
    query = " ".join(args.query)
    try:
        results = run_search(query, cfg)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not results:
        print(
            "No results found. If nothing is embedded yet, run `carta embed` first; "
            "otherwise try different wording."
        )
        return
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")

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
    run_bootstrap(Path.cwd())

def main():
    parser = argparse.ArgumentParser(prog="carta")
    parser.add_argument("--version", action="version", version=f"carta {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")
    sub.add_parser("scan")
    sub.add_parser("embed")
    search_p = sub.add_parser("search")
    search_p.add_argument("query", nargs="+")

    args = parser.parse_args()

    dispatch = {"init": cmd_init, "scan": cmd_scan, "embed": cmd_embed, "search": cmd_search}

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
