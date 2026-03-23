import argparse
import sys
from pathlib import Path
from carta import __version__

def find_config(start: Path = None) -> Path:
    root = start or Path.cwd()
    candidate = root / ".carta" / "config.yaml"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f".carta/config.yaml not found in {root}. Run `carta init` first."
    )

def cmd_scan(args):
    from carta.config import load_config
    from carta.scanner.scanner import run_scan
    import json
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    output_path = cfg_path.parent / "scan-results.json"
    results = run_scan(cfg_path.parent.parent, cfg)
    output_path.write_text(json.dumps(results, indent=2))
    issue_count = len(results["issues"])
    print(f"Scan complete: {issue_count} issue(s). Results at {output_path}")

def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed
    cfg = load_config(find_config())
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
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")

def cmd_init(args):
    from install.bootstrap import run_bootstrap
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
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
