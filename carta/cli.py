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


def _detect_ram_gb():
    """Best-effort total system RAM in GB. POSIX-only (Linux/macOS); None elsewhere."""
    try:
        if (hasattr(os, "sysconf")
                and "SC_PAGE_SIZE" in os.sysconf_names
                and "SC_PHYS_PAGES" in os.sysconf_names):
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except (ValueError, OSError):
        pass
    return None


def _recommend_vision_workers(ram_gb: float) -> int:
    """Heuristic: parallel qwen3-vl:8b slots that fit without thrashing.

    Reserves ~17 GB (≈8 GB OS + ≈9 GB OCR model resident) and assumes each
    additional vision-model parallel slot needs ~8 GB. Capped at 4 to keep
    the Ollama server from saturating regardless of RAM.
    """
    available = ram_gb - 17.0
    if available <= 0:
        return 1
    return max(1, min(4, int(available / 8.0)))


def _maybe_tune_workers(cfg: dict, skip: bool) -> dict:
    """Prompt to use RAM-recommended vision_workers if config differs.

    Quiet no-op when skipping, non-TTY, RAM detection fails, or already at
    the recommended value. Mutates cfg in-place for the current run only;
    does not write back to .carta/config.yaml.
    """
    if skip or os.environ.get("CARTA_NO_TUNE") == "1":
        return cfg
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return cfg
    ram_gb = _detect_ram_gb()
    if ram_gb is None:
        return cfg
    embed_cfg = cfg.setdefault("embed", {})
    current = int(embed_cfg.get("vision_workers", 4))
    recommended = _recommend_vision_workers(ram_gb)
    if current == recommended:
        return cfg

    print(
        f"\n  Detected {ram_gb:.0f} GB RAM. Recommended vision_workers={recommended} "
        f"(config has {current}).",
        flush=True,
    )
    sys.stdout.write(f"  Use {recommended} for this run? [Y/n] ")
    sys.stdout.flush()
    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return cfg
    if response in ("", "y", "yes"):
        embed_cfg["vision_workers"] = recommended
        print(f"  → Using vision_workers={recommended}\n", flush=True)
    else:
        print(f"  → Keeping vision_workers={current}\n", flush=True)
    return cfg


def _notify_if_update(cfg_path=None, cfg=None):
    """Call maybe_notify if we have a config context. Silently skips on error."""
    try:
        from carta.update.checker import maybe_notify
        carta_dir = cfg_path.parent if cfg_path else None
        maybe_notify(carta_dir, cfg or {})
    except Exception:
        pass


# The single-writer embed lock lives in carta.embed.lock so the CLI and the MCP
# carta_embed tool share one lock per project — exactly one writer per project so
# concurrent runs cannot delete each other's just-written points (audit CA-2/5/12).


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
    suggestions = results.get("related_suggestions") or []
    if suggestions:
        print()
        print("\U0001f4ce Suggested related: links (similarity \u2265 0.85):")
        for s in suggestions:
            print(f"  {s['doc']}: {s['suggested']} ({s['score']:.2f})")
    _notify_if_update(cfg_path, cfg)

def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed, run_embed_file
    from carta.embed.lock import acquire as _acquire_embed_lock, EmbedLockHeld
    from carta.ui import Progress
    import time

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_embed"):
        print("doc_embed module is disabled in config.", file=sys.stderr)
        sys.exit(1)

    # Best-effort: record this project in the global registry for `carta status`.
    try:
        from carta.registry import register_project
        register_project(cfg_path.parent.parent, cfg["project_name"], cfg.get("qdrant_url"))
    except Exception:
        pass

    # --timeout overrides embed.file_timeout_s
    timeout_override = getattr(args, "timeout", None)
    if timeout_override is not None:
        cfg.setdefault("embed", {})["file_timeout_s"] = timeout_override

    # Single-writer lock for ALL mutating embed paths — full pipeline, --repair,
    # --visual, and targeted --files. Previously only the full-pipeline branch
    # locked, so a --repair / --visual / --files run (or a concurrent MCP
    # carta_embed) could race its cleanup-delete against another writer and drop
    # freshly-written points (audit CA-2/5/12).
    lock_path = cfg_path.parent / "embed.lock"
    try:
        _acquire_embed_lock(lock_path)
    except EmbedLockHeld as e:
        print(
            f"carta embed is already running (PID: {e.pid}). Wait for it to finish "
            f"or remove .carta/embed.lock if it is stale.",
            file=sys.stderr,
        )
        sys.exit(1)

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

    # --repair: detect and fix corpus-integrity issues, then exit.
    # `is True` (not `if args.repair`) — rejects truthy MagicMocks in tests.
    if getattr(args, "repair", False) is True:
        from carta.embed.repair import run_repair
        repo_root = cfg_path.parent.parent
        summary = run_repair(repo_root, cfg, verbose=True)
        _notify_if_update(cfg_path, cfg)
        sys.exit(1 if summary["failed"] else 0)

    # --visual: slow pass-2 drainer — OCR text + ColPali per pending page, then exit.
    # `is True` (not `if args.visual`) — rejects truthy MagicMocks in tests.
    if getattr(args, "visual", False) is True:
        from carta.embed.pipeline import run_visual_embed
        repo_root = cfg_path.parent.parent
        summary = run_visual_embed(repo_root, cfg, verbose=True)
        status = summary.get("status", "")
        if status == "visual_unavailable":
            sys.exit(1)
        print(
            f"carta embed --visual: {summary['pages_embedded']} page(s) embedded, "
            f"{summary['pages_failed']} failed, across {summary['files']} file(s).",
            flush=True,
        )
        _notify_if_update(cfg_path, cfg)
        sys.exit(1 if summary["pages_failed"] else 0)

    # Suggest a vision_workers value that fits this machine's RAM (interactive only).
    cfg = _maybe_tune_workers(cfg, skip=getattr(args, "no_tune", False))

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

    # Lock already acquired at the top of cmd_embed (covers all mutating branches).
    repo_root = cfg_path.parent.parent

    with Progress() as progress:
        summary = run_embed(repo_root, cfg, verbose=False, progress=progress)
    progress.summary(
        embedded=summary["embedded"],
        skipped=summary["skipped"],
        errors=len(summary["errors"]),
    )
    failed_extractions = summary.get("extraction_failed", 0)
    if failed_extractions:
        print(
            f"\nWarning: {failed_extractions} file(s) yielded no extractable text "
            f"(scanned PDFs? OCR may be required) — flagged extraction_failed, "
            f"nothing embedded for them.",
            file=sys.stderr,
        )
    timed_out = summary.get("timed_out", [])
    if timed_out:
        current = cfg.get("embed", {}).get("file_timeout_s", 600)
        suggested = current * 2
        print(
            f"\nHint: {len(timed_out)} file(s) timed out at {current}s. "
            f"Re-run with --timeout {suggested} to give them more time.",
            file=sys.stderr,
        )
    failed = summary.get("failed", [])
    partial = summary.get("partial", [])
    if failed or partial:
        print(
            f"\nWarning: {len(failed)} file(s) failed to embed and {len(partial)} "
            f"only partially embedded (transient Ollama/Qdrant errors?). They were NOT "
            f"marked done — re-run `carta embed` to retry them.",
            file=sys.stderr,
        )
    _notify_if_update(cfg_path, cfg)
    # Exit non-zero on errors OR incomplete embeds so a bulk re-embed can never
    # report false-green when files silently failed/partially embedded.
    if summary["errors"] or failed or partial:
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
    from carta.config import NOTE_DOC_TYPES
    for r in results:
        tag = f"[{r['doc_type']}] " if r.get("doc_type") in NOTE_DOC_TYPES else ""
        print(f"[{r['score']:.2f}] {tag}{r['source']} — {r['excerpt']}")

    hops = getattr(args, "hops", 0)
    if hops > 0:
        from carta.search.graph import build_related_graph, walk_hops
        repo_root = cfg_path.parent.parent
        docs_root_rel = cfg.get("docs_root", "docs/").rstrip("/")
        graph = build_related_graph(repo_root, repo_root / docs_root_rel)
        seeds = [r["source"] for r in results if r.get("source")]
        hop_results = walk_hops(seeds, graph, hops)
        if hop_results:
            print()
            print(f"\U0001f310 Related via graph ({hops}-hop expansion):")
            for h in hop_results:
                print(f"  [{h['hop']}-hop] {h['doc']}  (via {h['via']})")

    _notify_if_update(cfg_path, cfg)


def cmd_focus(args):
    from carta.config import load_config
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_focus
    repo_root = cfg_path.parent.parent
    query = " ".join(args.query) if args.query else ""
    try:
        results = run_focus(args.source, cfg, query=query, limit=args.limit)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print(f"No focus results for {args.source!r}. Is the file embedded? "
              f"Use `carta search` to find the exact source path.")
        _notify_if_update(cfg_path, cfg)
        return

    if not query:
        print(f"Outline of {args.source} ({len(results)} sections):")
        for r in results:
            page = r.get("page")
            page_s = f"p.{page}" if page is not None else "p.?"
            heading = r.get("section_heading") or "(no heading)"
            print(f"  {page_s:>6}  {heading}")
        _notify_if_update(cfg_path, cfg)
        return

    cache_dir = repo_root / ".carta" / "cache" / "focus"
    stem = Path(args.source).stem
    for r in results:
        page = r.get("page")
        page_s = f"p.{page}" if page is not None else "p.?"
        heading = r.get("section_heading") or ""
        head_s = f" §{heading}" if heading else ""
        print(f"[{r['score']:.2f}] {r['source']} {page_s}{head_s} — {r['excerpt']}")
        if r.get("image_b64"):
            import base64
            cache_dir.mkdir(parents=True, exist_ok=True)
            img_path = cache_dir / f"{stem}-p{page}.png"
            img_path.write_bytes(base64.b64decode(r["image_b64"]))
            print(f"        ↳ page image: {img_path}")
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

    # Offer to wire the embed-progress segment into the user's status line.
    try:
        from carta import statusline
        result = statusline.offer_install(interactive=sys.stdin.isatty())
        if result == "installed":
            print("✓ Wired carta embed-progress into your status line.")
        elif result == "unsupported":
            print(
                "Note: couldn't auto-wire the status line; add this before your "
                'script prints $parts:\n'
                '  seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)\n'
                '  [ -n "$seg" ] && parts="$parts │ $seg"'
            )
    except Exception:
        pass  # status-line wiring is a convenience, never block init

    # Best-effort: record the freshly-initialised project for `carta status`.
    try:
        from carta.config import load_config
        from carta.registry import register_project
        cp = find_config(Path.cwd())
        c = load_config(cp)
        register_project(cp.parent.parent, c["project_name"], c.get("qdrant_url"))
    except Exception:
        pass

    _notify_if_update()

def cmd_statusline(args):
    """Print the embed-progress status-line segment, or install/uninstall wiring."""
    from carta import statusline

    if getattr(args, "install", False) or getattr(args, "uninstall", False):
        settings_path = Path.home() / ".claude" / "settings.json"
        script = statusline.find_statusline_script(settings_path)
        if script is None:
            print(
                "carta statusline: no wireable status-line script found in "
                f"{settings_path}.\n"
                "Add this to your status-line script, before it prints $parts:\n"
                '  seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)\n'
                '  [ -n "$seg" ] && parts="$parts │ $seg"',
                file=sys.stderr,
            )
            sys.exit(1)
        if getattr(args, "uninstall", False):
            result = statusline.uninstall_from_script(script)
            print(f"carta statusline: {result} ({script})")
            sys.exit(0)
        result = statusline.install_into_script(
            script, confirm=lambda msg: input(f"{msg} [y/N] ").strip().lower() == "y"
        )
        print(f"carta statusline: {result} ({script})")
        if result == "installed":
            print(f"  backup: {script}.bak")
        sys.exit(0)

    # Default: print the segment for the current working directory.
    statusline.print_segment()
    sys.exit(0)


def cmd_doctor(args):
    """Run diagnostic checks and optionally auto-fix issues."""
    from carta.install.preflight import PreflightChecker, PreflightResult
    from carta.install.auto_fix import AutoInstaller
    interactive = not (args.yes or args.fix)  # --yes or --fix disables prompts
    checker = PreflightChecker(interactive=interactive, verbose=args.verbose, project_root=Path.cwd())
    result = checker.run()

    # Print human-readable report (JSON deferred until after corpus-integrity merge)
    if not args.json:
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

    # Corpus integrity (project-scoped, read-only). Never break doctor itself.
    try:
        cfg_path = find_config()
    except Exception:
        cfg_path = None
    if cfg_path is not None:
        import json as _json
        try:
            from carta.config import load_config
            from carta.embed.integrity import scan_corpus_integrity
            cfg = load_config(cfg_path)
            repo_root = cfg_path.parent.parent
            report = scan_corpus_integrity(cfg, repo_root)
            if args.json:
                doc = result.to_dict()
                doc["corpus_integrity"] = report
                print(_json.dumps(doc, indent=2))
            else:
                print("\n📦 Corpus integrity")
                visual_mm = report.get("visual_count_mismatches", {})
                orphan_vis = report.get("orphaned_visual_files", [])
                if (not report["affected_files"] and not report["stuck_stale"]
                        and not visual_mm and not orphan_vis):
                    print("  ✅ no issues found")
                else:
                    for slug, files in report["slug_collisions"].items():
                        print(f"  ⚠️  slug collision '{slug}': {', '.join(files)}")
                    for fp in report["empty_files"]:
                        print(f"  ⚠️  all chunks empty: {fp}")
                    for fp, n in report["partial_empty_files"].items():
                        print(f"  ⚠️  {n} empty chunk(s): {fp}")
                    for fp, c in report["count_mismatches"].items():
                        print(f"  ⚠️  count mismatch: {fp} (sidecar {c['sidecar']} vs qdrant {c['qdrant']})")
                    if report["stuck_stale"]:
                        print(f"  ⚠️  {len(report['stuck_stale'])} sidecar(s) stuck 'stale' with unchanged files")
                    for fp, c in visual_mm.items():
                        print(f"  ⚠️  visual count mismatch: {fp} (sidecar {c['sidecar']} vs qdrant {c['qdrant']})")
                    for fp in orphan_vis:
                        print(f"  ⚠️  orphaned visual points: {fp}")
                    print("  → run `carta embed --repair` to fix")
        except Exception as e:
            if args.json:
                # Still emit one valid JSON document; note the skipped check
                # instead of leaving consumers with empty stdout.
                doc = result.to_dict()
                doc["corpus_integrity"] = {"skipped": str(e)}
                print(_json.dumps(doc, indent=2))
            else:
                print(f"\n📦 Corpus integrity: check skipped ({e})")
    elif args.json:
        # Outside a project: emit the preflight JSON with no corpus_integrity key
        print(result.to_json())

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

def cmd_eval(args):
    """Score retrieval quality against an eval set (recall@k, MRR).

    Eval is currently repo-scoped only. Scope-aware eval is a follow-up that
    requires run_search to accept a scope parameter.
    """
    import copy
    from carta.config import load_config
    from carta.eval.harness import run_eval
    from carta.embed.pipeline import run_search

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    k = args.k

    # Deep-copy cfg once; the closure mutates top_n per call so each query
    # uses the correct top_k cutoff. run_search reads cfg["search"]["top_n"]
    # internally; results use "source" as the file path key (not "file_path"),
    # so the closure remaps to "file_path".
    eval_cfg = copy.deepcopy(cfg)
    rerank_requested = bool(eval_cfg.get("search", {}).get("rerank", {}).get("enabled", False))
    rerank_applied_count = 0
    query_count = 0

    def _search(query: str, top_k: int) -> list:
        nonlocal rerank_applied_count, query_count
        eval_cfg.setdefault("search", {})["top_n"] = top_k
        stats: dict = {}
        results = run_search(query, eval_cfg, stats=stats) or []
        query_count += 1
        if stats.get("rerank_applied"):
            rerank_applied_count += 1
        # run_search returns {"score", "source", "excerpt", "type"};
        # run_eval expects dicts with "file_path".
        return [{"file_path": r.get("source", ""), **r} for r in results]

    metrics = run_eval(Path(args.eval_path), _search, k=k)
    print(f"queries={metrics['n_queries']}  recall@{k}={metrics['recall_at_k']:.3f}  MRR={metrics['mrr']:.3f}")
    if rerank_requested:
        print(f"rerank: applied on {rerank_applied_count}/{query_count} queries")
        # Partial fail-open: a 0.8B reranker degrading on N/Q queries still prints a
        # mostly-reranked score that an operator may trust for a go/no-go call. Make
        # the partial case a loud stderr warning so it can't be mistaken for clean
        # (the total-fail-open case below hard-fails) — audit CA-20.
        if query_count and 0 < rerank_applied_count < query_count:
            print(
                f"Warning: reranker failed open on {query_count - rerank_applied_count}/"
                f"{query_count} queries — these are PARTIALLY reranked numbers, not a "
                f"clean reranked run; treat the score with caution.",
                file=sys.stderr,
            )
    else:
        print("rerank: not requested")
    for row in metrics["per_query"]:
        mark = row["first_hit_rank"] if row["first_hit_rank"] is not None else "MISS"
        print(f"  [{mark}] {row['q']}")

    # A reranker that failed open on EVERY query is indistinguishable from a
    # working one in rank metrics alone — that's how 0.8.0 shipped broken.
    # Make it impossible to mistake for a result.
    if rerank_requested and query_count and rerank_applied_count == 0:
        print(
            "Error: search.rerank.enabled is true but the reranker ran on 0 queries — "
            "it is silently failing open (check the model and search.rerank.* config). "
            "These are NOT reranked numbers.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_remember(args):
    """Save a curated project note (quirk/bug-note/helpful-note) and embed it."""
    from carta.config import load_config
    from carta.memory.capture import capture_note
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()] or None
    try:
        result = capture_note(cfg, repo_root, args.text, note_type=args.type,
                              title=args.title, tags=tags)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Note saved: {result['path']} → {result['collection']} "
          f"({result['chunks']} chunks)")


def cmd_export(args):
    """Bundle this project's embeddings into a portable .tar.gz for sharing."""
    from carta.config import load_config
    from carta.share import run_export
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    try:
        run_export(
            cfg,
            cfg_path.parent,
            output_path=args.output,
            include_visual=not args.no_visual,
            verbose=True,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_import(args):
    """Restore a shared embeddings bundle into the local Qdrant and wire up .carta/."""
    from carta.share import run_import
    try:
        cfg_path = find_config()
        carta_dir = cfg_path.parent
        from carta.config import load_config
        qdrant_url = load_config(cfg_path).get("qdrant_url")
    except FileNotFoundError:
        # Fresh machine, no local config yet — restore into ./.carta and let
        # run_import read qdrant_url from the bundled config.
        carta_dir = Path.cwd() / ".carta"
        qdrant_url = None
    try:
        run_import(
            args.bundle,
            carta_dir,
            qdrant_url=qdrant_url,
            project=args.project,
            force=args.force,
            verbose=True,
        )
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    """Print a quick snapshot of carta state: current project + other projects."""
    import json as _json
    from carta.config import load_config
    from carta.registry import register_project, load_registry
    from carta import status as status_mod

    check = getattr(args, "check", False)
    as_json = getattr(args, "json", False)
    color = sys.stdout.isatty() and not as_json

    try:
        cfg_path = find_config()
    except FileNotFoundError:
        cfg_path = None

    current = None
    current_path = None
    if cfg_path is not None:
        cfg = load_config(cfg_path)
        repo_root = cfg_path.parent.parent
        current_path = str(repo_root.resolve())
        name = cfg["project_name"]
        qdrant_url = cfg.get("qdrant_url")
        ollama_url = cfg.get("embed", {}).get("ollama_url", "http://localhost:11434")
        try:
            register_project(repo_root, name, qdrant_url)
        except Exception:
            pass
        current = status_mod.gather_project_status(
            repo_root, name=name, qdrant_url=qdrant_url,
            check=check, ollama_url=ollama_url,
        )

    others = []
    for entry in sorted(load_registry(), key=lambda e: e["last_seen"], reverse=True):
        if current_path and str(Path(entry["path"]).resolve()) == current_path:
            continue
        others.append(status_mod.gather_project_status(
            Path(entry["path"]), name=entry["name"], qdrant_url=entry["qdrant_url"],
        ))

    if as_json:
        print(_json.dumps(
            {"current": current, "others": others, "checked": bool(check)}, indent=2
        ))
        return

    if current is None and not others:
        print("Not inside a carta project, and none registered yet — "
              "run a carta command inside a project first.")
        return

    if current is not None:
        print(status_mod.format_current(current, color=color))
    if others:
        if current is not None:
            print()
        print(f"Other projects ({len(others)}):")
        for snap in others:
            print(status_mod.format_other(snap, color=color))


def _print_stale_result(result, scfg):
    if not result.findings:
        return
    print(f"carta stale-scan: scanned {result.scanned} doc(s)...", file=sys.stderr)
    for f in result.findings:
        section = f"Section \"{f.section}\" " if f.section and f.section != "(intro)" else ""
        print(f"  ⚠  {f.file}", file=sys.stderr)
        print(
            f"     {section}may be stale — knowledge base suggests it was replaced "
            f"({f.candidate_path}, score {f.candidate_score:.2f}).",
            file=sys.stderr,
        )
        if f.section and f.section != "(intro)":
            print(f"     Run: /doc-search \"{f.section.lstrip('# ').strip()}\"", file=sys.stderr)
    if result.skipped_overflow:
        print(f"  ({result.skipped_overflow} more section(s) not checked — max_judge_calls cap)", file=sys.stderr)
    if not scfg.get("block_on_stale", False):
        print("  (warn-only; set hooks.stale_scan.block_on_stale: true to fail)", file=sys.stderr)


def cmd_hook(args):
    import subprocess as _subprocess

    action = getattr(args, "hook_action", None)

    # install/uninstall need only the git root — NOT a full Carta setup, so a
    # user can install the hook before/independent of `carta init`.
    if action == "install":
        from carta.hook.git_hook import install_hook, uninstall_hook
        try:
            repo_root = Path(_subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            ).stdout.strip())
        except (_subprocess.CalledProcessError, FileNotFoundError):
            print("Not a git repository.", file=sys.stderr)
            sys.exit(1)
        stage = args.stage
        if getattr(args, "uninstall", False):
            print(f"carta hook ({stage}): {uninstall_hook(repo_root, stage)}")
            return
        try:
            status = install_hook(repo_root, stage)
        except FileExistsError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"carta hook ({stage}): {status} → .git/hooks/{stage}")
        return

    if action == "check":
        from carta.config import load_config
        from carta.hook import stale_scan
        try:
            cfg_path = find_config()
        except FileNotFoundError:
            sys.exit(0)  # not a Carta repo → nothing to check, fail-open
        cfg = load_config(cfg_path)
        repo_root = cfg_path.parent.parent
        scfg = cfg.get("hooks", {}).get("stale_scan", {})
        if not scfg.get("enabled", True):
            sys.exit(0)
        stage = args.stage
        diff = getattr(args, "diff", None)
        try:
            if diff is not None:
                range_spec = diff or f"{stale_scan._default_branch(repo_root)}...HEAD"
                docs = stale_scan.collect_range(repo_root, cfg, range_spec)
            elif stage == "pre-commit":
                docs = stale_scan.collect_staged(repo_root, cfg)
            else:
                stdin_lines = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
                docs = stale_scan.collect_pushed(repo_root, cfg, stdin_lines)
        except Exception as e:
            print(f"carta stale-scan: collection error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        if not docs:
            sys.exit(0)
        try:
            result = stale_scan.run_stale_scan(repo_root, cfg, docs)
        except Exception as e:
            print(f"carta stale-scan: scan error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        _print_stale_result(result, scfg)
        fail = getattr(args, "fail_on_stale", False) or scfg.get("block_on_stale", False)
        if result.findings and fail:
            sys.exit(1)
        sys.exit(0)

    print("usage: carta hook {install,check} [--stage pre-push|pre-commit]", file=sys.stderr)
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
        help="Specific file(s) to embed immediately (skips the discovery scan)",
    )
    embed_p.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        help="Per-file timeout for embedding. Overrides embed.file_timeout_s in config.",
    )
    embed_p.add_argument(
        "--no-tune",
        action="store_true",
        help="Skip the RAM-based vision_workers tuning prompt.",
    )
    embed_p.add_argument(
        "--visual",
        action="store_true",
        help="Run the slow visual pass: drain visual_pending pages (OCR text + ColPali).",
    )
    embed_p.add_argument(
        "--repair",
        action="store_true",
        help="Detect and repair corpus-integrity issues (point-ID collisions, "
             "empty chunks, count mismatches), then exit.",
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
    search_p.add_argument(
        "--hops",
        type=int,
        default=0,
        metavar="N",
        help=(
            "After returning semantic results, also return docs linked via related: "
            "within N hops of each result (default: 0 = no graph expansion)"
        ),
    )

    focus_p = sub.add_parser(
        "focus",
        help="Go deep in one file: page-anchored passages, or an outline (omit the query)")
    focus_p.add_argument("query", nargs="*",
                         help="Query to search within the file; omit for a section/page outline")
    focus_p.add_argument("--source", required=True, metavar="PATH",
                         help="Repo-relative file path (the 'source' from a carta search result)")
    focus_p.add_argument("--limit", type=int, default=15,
                         help="Max passages to return (default 15)")

    eval_p = sub.add_parser("eval", help="Score retrieval quality against an eval set")
    eval_p.add_argument("eval_path", help="Path to eval-set YAML (see carta/eval/datasets/example.yaml)")
    eval_p.add_argument("-k", type=int, default=5, help="top-k cutoff (default 5)")

    remember_p = sub.add_parser(
        "remember",
        help="Save a project note (quirk/bug-note/helpful-note) and embed it",
    )
    remember_p.add_argument("text", help="The note text")
    remember_p.add_argument(
        "--type", choices=["quirk", "bug-note", "helpful-note"],
        default="helpful-note", help="Note type (default: helpful-note)",
    )
    remember_p.add_argument("--title", default="", help="Optional title (drives the filename slug)")
    remember_p.add_argument("--tags", default="", help="Comma-separated tags")

    update_p = sub.add_parser("update", help="Update carta to the latest version")
    update_p.add_argument("--check", action="store_true", help="Show available version without upgrading")
    update_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    statusline_p = sub.add_parser(
        "statusline",
        help="Print the embed-progress status-line segment (or --install/--uninstall wiring)",
    )
    statusline_grp = statusline_p.add_mutually_exclusive_group()
    statusline_grp.add_argument(
        "--install", action="store_true",
        help="Wire the carta segment into your Claude Code status-line script",
    )
    statusline_grp.add_argument(
        "--uninstall", action="store_true",
        help="Remove the carta segment from your status-line script",
    )

    export_p = sub.add_parser(
        "export",
        help="Bundle this project's embeddings into a portable .tar.gz to share",
    )
    export_p.add_argument(
        "-o", "--output", metavar="PATH",
        help="Output bundle path (default: ./carta-<project>-<date>.tar.gz)",
    )
    export_p.add_argument(
        "--no-visual", action="store_true",
        help="Exclude the _visual (ColPali) collection from the bundle",
    )

    import_p = sub.add_parser(
        "import",
        help="Restore a shared embeddings bundle into the local Qdrant",
    )
    import_p.add_argument("bundle", help="Path to a carta export .tar.gz bundle")
    import_p.add_argument(
        "--project", metavar="NAME",
        help="Restore under a different project name (rewrites collection names)",
    )
    import_p.add_argument(
        "--force", action="store_true",
        help="Overwrite any collections that already exist",
    )

    status_p = sub.add_parser(
        "status",
        help="Show carta status for this project and other known projects",
    )
    status_p.add_argument(
        "--check", action="store_true",
        help="Also query Qdrant/Ollama for the current project (live counts + health)",
    )
    status_p.add_argument(
        "--json", action="store_true", help="Output status as JSON",
    )

    hook_p = sub.add_parser("hook", help="Manage Carta git hooks (stale-reference scan)")
    hook_sub = hook_p.add_subparsers(dest="hook_action")
    hook_install = hook_sub.add_parser("install", help="Install/remove the managed git hook")
    hook_install.add_argument("--stage", choices=["pre-push", "pre-commit"], default="pre-push")
    hook_install.add_argument("--uninstall", action="store_true", help="Remove the managed hook")
    hook_check = hook_sub.add_parser("check", help="Run the stale-reference scan (used by the git shim)")
    hook_check.add_argument("--stage", choices=["pre-push", "pre-commit"], default="pre-push")
    hook_check.add_argument(
        "--diff", nargs="?", const="", default=None, metavar="RANGE",
        help="Scan docs changed across a git range instead of staged/pushed "
             "(bare --diff uses <default-branch>...HEAD)",
    )
    hook_check.add_argument(
        "--fail-on-stale", action="store_true",
        help="Exit 1 if any stale finding (default: warn-only, exit 0)",
    )

    args = parser.parse_args()

    dispatch = {
        "init": cmd_init,
        "scan": cmd_scan,
        "embed": cmd_embed,
        "search": cmd_search,
        "focus": cmd_focus,
        "audit": cmd_audit,
        "doctor": cmd_doctor,
        "eval": cmd_eval,
        "remember": cmd_remember,
        "update": cmd_update,
        "statusline": cmd_statusline,
        "export": cmd_export,
        "import": cmd_import,
        "status": cmd_status,
        "hook": cmd_hook,
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
