import argparse
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
    import json
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_audit"):
        print("doc_audit module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    output_path = cfg_path.parent / "scan-results.json"
    results = run_scan(cfg_path.parent.parent, cfg)
    output_path.write_text(json.dumps(results, indent=2))
    issue_count = len(results["issues"])
    print(f"Scan complete: {issue_count} issue(s). Results at {output_path}")

def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed
    cfg = load_config(find_config())
    if not cfg["modules"].get("doc_embed"):
        print("doc_embed module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    summary = run_embed(Path.cwd(), cfg)
    print(f"Embedded: {summary['embedded']}, Skipped: {summary['skipped']}")
    if summary["errors"]:
        for err in summary["errors"]:
            print(f"  ERROR: {err}", file=sys.stderr)
        sys.exit(1)

def cmd_search(args):
    from carta.config import load_config
    cfg = load_config(find_config())
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_search
    query = " ".join(args.query)
    results = run_search(query, cfg)
    if not results:
        print("No results found.")
        return
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")

def cmd_init(args):
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
