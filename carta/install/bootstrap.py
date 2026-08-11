import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml

CARTA_RUNTIME_SRC = Path(__file__).parent.parent

VECTOR_DIMENSIONS = {"doc": 768, "session": 768, "notes": 768}


def _is_interactive() -> bool:
    """Check if running in an interactive terminal (not in tests/CI)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_user(message: str, default: bool = True) -> bool:
    """Prompt user for Y/n input, handling non-interactive environments."""
    if not _is_interactive():
        return default

    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        response = input(message + suffix).strip().lower()
    except (EOFError, OSError):
        # Non-interactive environment (tests, CI)
        return default

    if default:
        return response not in ("n", "no", "false")
    else:
        return response in ("y", "yes", "true")


def _skills_source_dir(project_root: Path) -> Path:
    """Directory containing Claude Code skills shipped with Carta.

    Prefer repo-local `skills/` when present, otherwise fall back to the installed
    package's `carta/skills/` directory.
    """
    repo_skills = project_root / "skills"
    if repo_skills.is_dir():
        return repo_skills
    return CARTA_RUNTIME_SRC / "skills"


def _skills_destination_root(choice: str, project_root: Path) -> Path:
    """Root directory for Claude Code skills: global ~/.claude/skills or project .claude/skills."""
    if choice == "G":
        return Path.home() / ".claude" / "skills"
    if choice == "P":
        return project_root / ".claude" / "skills"
    raise ValueError(f"Invalid skills choice: {choice!r}")


def _prompt_skills_choice() -> str:
    """Interactive G/P/S for skill installation. Default G."""
    if not _is_interactive():
        return "G"
    try:
        print("Install Carta skills? [G]lobal/[P]roject/[S]kip [G]: ", end="", flush=True)
        line = input().strip().lower()
    except (EOFError, OSError):
        return "G"
    if not line or line in ("g", "global"):
        return "G"
    if line in ("p", "project"):
        return "P"
    if line in ("s", "skip"):
        return "S"
    return "G"


def _install_skills(choice: str, project_root: Path) -> tuple[int, int, str]:
    """Copy each `*/SKILL.md` folder into Claude skill layout. Idempotent per skill.

    Returns:
        (copied_count, already_present_count, display_path): new copies, skips because file
        already existed, and a short path for messages (e.g. ~/.claude/skills or .claude/skills).
        display_path is "" if nothing to report (missing source / empty dir).
    """
    source_dir = _skills_source_dir(project_root)
    if not source_dir.is_dir():
        print(
            f"  Warning: skill sources not found ({source_dir}); skipping skill install.",
            file=sys.stderr,
        )
        return (0, 0, "")

    dest_root = _skills_destination_root(choice, project_root)
    skill_dirs = sorted([p for p in source_dir.iterdir() if p.is_dir()])
    if not skill_dirs:
        print(f"  Warning: no skill dirs in {source_dir}; skipping skill install.", file=sys.stderr)
        return (0, 0, "")

    copied = 0
    already = 0
    for src_dir in skill_dirs:
        skill_name = src_dir.name
        src_skill = src_dir / "SKILL.md"
        if not src_skill.is_file():
            # Not a Claude Code skill folder
            continue

        dest_dir = dest_root / skill_name
        dest_skill = dest_dir / "SKILL.md"
        if dest_skill.is_file():
            already += 1
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Copy the whole folder so future auxiliary files are preserved.
            shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
            copied += 1
        except OSError as e:
            print(f"  Warning: could not install skill {skill_name}: {e}", file=sys.stderr)

    if choice == "G":
        display = "~/.claude/skills"
    else:
        display = ".claude/skills"
    return (copied, already, display)


def run_bootstrap(project_root: Path, *, skip_skills: bool = False) -> None:
    """Bootstrap Carta in a project with comprehensive preflight checks."""
    # Phase 0: Run comprehensive preflight checks
    from carta.install.preflight import PreflightChecker
    from carta.install.auto_fix import AutoInstaller

    interactive = _is_interactive()

    print("🔍 Running preflight checks...")
    checker = PreflightChecker(interactive=interactive, verbose=False, project_root=project_root)
    result = checker.run()

    # Print report
    result.print_report(verbose=False)

    # Handle fixable failures
    if result.fixable_failures and interactive:
        print(f"\n🔧 {len(result.fixable_failures)} issue(s) can be auto-fixed.")

        # Prompt user for auto-fix
        if _prompt_user("Attempt to auto-fix issues?", default=True):
            installer = AutoInstaller(interactive=interactive, verbose=False)
            fixes = installer.fix_all(result)

            successful = sum(1 for success in fixes.values() if success)
            print(f"\n✅ Fixed: {successful}/{len(fixes)}")

            # Re-run checks to verify
            if successful > 0:
                print("\n🔄 Re-running checks to verify...")
                result = checker.run()
                result.print_report(verbose=False)
    elif result.fixable_failures and not interactive:
        # In non-interactive mode, print instructions but don't auto-fix
        print(f"\n🔧 {len(result.fixable_failures)} issue(s) can be auto-fixed.")
        print("   Run 'carta doctor --fix' to fix automatically.")

    # Handle critical failures (block initialization)
    if not result.can_proceed():
        print("\n" + "━" * 55)
        print("🔴 Critical issues must be resolved before Carta can be initialized.")
        print("\nOptions:")
        print("  1. Run 'carta doctor --fix' to attempt automatic fixes")
        print("  2. Run 'carta doctor' to see detailed setup instructions")
        print("  3. Resolve issues manually and re-run 'carta init'")
        print("\n" + "━" * 55)

        # Print setup guide
        installer = AutoInstaller(interactive=False)
        installer.print_setup_guide(result)
        sys.exit(1)

    # Extract check results for module configuration
    qdrant_ok = any(
        c.name == "qdrant_running" and c.status == "pass"
        for c in result.checks
    )
    ollama_ok = any(
        c.name == "ollama_running" and c.status == "pass"
        for c in result.checks
    )

    # Continue with initialization
    project_name = _detect_project_name(project_root)
    print(f"\nInitialising Carta for project: {project_name}")

    qdrant_url = os.environ.get("CARTA_QDRANT_URL", "http://localhost:6333")
    if qdrant_ok:
        print(f"  Qdrant ready at {qdrant_url}")

    modules = {
        "doc_audit": True,
        "doc_embed": qdrant_ok,
        "doc_search": qdrant_ok,
        "session_memory": True,
        "proactive_recall": ollama_ok,
    }

    ollama_url = os.environ.get("CARTA_OLLAMA_URL", "http://localhost:11434")
    if ollama_ok:
        print(f"  Ollama ready at {ollama_url}")

    carta_dir = project_root / ".carta"
    carta_dir.mkdir(exist_ok=True)
    _write_config(carta_dir, project_name, qdrant_url, modules)

    _register_hooks(project_root)
    if not _remove_plugin_cache():
        print(
            "  carta init aborted: stale plugin cache residue remains. "
            "Remove the directories listed above manually, then re-run carta init.",
            file=sys.stderr,
        )
        sys.exit(1)
    collections_ok = _create_qdrant_collections(project_name, qdrant_url)
    _update_gitignore(project_root)
    _create_mcp_configs(project_root)

    _append_claude_md(project_root, project_name)
    _create_agents_md(project_root, project_name)

    if not skip_skills:
        if _is_interactive():
            sk_choice = _prompt_skills_choice()
            if sk_choice != "S":
                copied, already, display = _install_skills(sk_choice, project_root)
                if display and (copied > 0 or already > 0):
                    msg_parts = []
                    if copied:
                        msg_parts.append(f"{copied} installed")
                    if already:
                        msg_parts.append(f"{already} already present")
                    print(f"\n✓ Carta skills at {display}: {', '.join(msg_parts)}")
                    print("  (Reload Claude Code to activate)")
        else:
            copied, already, display = _install_skills("G", project_root)
            if display and (copied > 0 or already > 0):
                msg_parts = []
                if copied:
                    msg_parts.append(f"{copied} installed")
                if already:
                    msg_parts.append(f"{already} already present")
                print(f"\n✓ Carta skills at {display}: {', '.join(msg_parts)}")
                print("  (Reload Claude Code to activate)")

    colls = f"{project_name}_doc, {project_name}_session, {project_name}_notes"
    if collections_ok:
        print(f"\n✅ Carta ready. Collections: {colls}")
        print("  Slash commands available: /doc-audit, /doc-embed, /doc-search")
        print("  (Reload Claude Code window to activate skills)")
    else:
        print(f"\n⚠️  Carta initialised but Qdrant collections could not be created.")
        print(f"  Expected collections: {colls}")
        print("  Fix the Qdrant errors above, then re-run: carta init")


def _detect_project_name(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=str(root), timeout=5,
        )
        if result.returncode == 0:
            name = Path(result.stdout.strip()).name
            if name:
                return name
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    name = root.name
    return name if name else "carta-project"


def _write_config(carta_dir: Path, project_name: str, qdrant_url: str, modules: dict) -> None:
    from carta.config import DEFAULTS, _deep_merge
    base = _deep_merge(DEFAULTS, {"modules": modules})

    # Re-running `carta init` must not clobber a user's tuned config (bootstrap
    # itself advises re-running on Qdrant errors). Overlay any existing config on
    # top of the defaults so user tuning survives, then force the freshly-computed
    # identity + modules so a re-run still reflects current detection.
    cfg_path = carta_dir / "config.yaml"
    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text()) or {}
            if isinstance(existing, dict):
                base = _deep_merge(base, existing)
        except (OSError, yaml.YAMLError):
            pass  # unreadable/corrupt existing config — fall back to fresh defaults
    base["modules"] = modules

    # Hoist identity fields to the top for readability
    ordered = {
        "project_name": project_name,
        "qdrant_url": qdrant_url,
        **{k: v for k, v in base.items() if k not in ("project_name", "qdrant_url")},
    }
    cfg_path.write_text(yaml.dump(ordered, default_flow_style=False, sort_keys=False))


def _register_hooks(project_root: Path) -> None:
    """Copy hook scripts to .carta/hooks/. Claude Code hook registration is now
    handled plugin-natively via hooks/hooks.json; we no longer write .claude/settings.json."""
    hooks_src = Path(__file__).parent.parent / "hooks"
    hooks_dest = project_root / ".carta" / "hooks"
    hooks_dest.mkdir(parents=True, exist_ok=True)
    for script in hooks_src.glob("*.sh"):
        dest_script = hooks_dest / script.name
        shutil.copy2(script, dest_script)
        dest_script.chmod(dest_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _remove_plugin_cache() -> bool:
    """Remove all Carta plugin cache directories from v0.1.x installations.

    Removes both known cache paths:
    - ~/.claude/plugins/carta/          (old v0.1.x path)
    - ~/.claude/plugins/cache/carta-cc/ (current cache path)

    Returns True if cleanup succeeded (no residue), False if residue remains.
    """
    paths_to_remove = [
        Path.home() / ".claude/plugins/carta",
        Path.home() / ".claude/plugins/cache/carta-cc",
    ]
    for p in paths_to_remove:
        if p.exists():
            try:
                shutil.rmtree(p)
                print(f"  Removed stale plugin cache: {p}")
            except OSError as e:
                print(f"  Warning: failed to remove {p}: {e}", file=sys.stderr)

    # Post-deletion assertion
    residue = [p for p in paths_to_remove if p.exists()]
    if residue:
        print(
            f"  ERROR: plugin cache residue remains after cleanup: {residue}\n"
            f"  Remove manually before using carta-mcp.",
            file=sys.stderr,
        )
        return False
    return True


def _create_qdrant_collections(project_name: str, qdrant_url: str, vector_size: int = 768) -> bool:
    """Create Qdrant collections with the schema the embed pipeline expects.

    Routes through carta.embed.embed.ensure_collection — the SAME code path
    `carta embed` uses — so init and embed can never create divergent schemas.
    (Init previously PUT a raw unnamed-dense collection via HTTP; embed creates a
    named hybrid dense+bm25 collection. Because ensure_collection skips existing
    collections, an init-before-embed project was permanently stuck on dense-only
    retrieval with BM25+RRF hybrid silently off — audit CA-10.)

    An existing collection with a legacy (non-hybrid) schema is left untouched but
    flagged loudly rather than silently rubber-stamped (audit CA-27).

    Returns True if all collections are present with no hard failure.
    """
    from qdrant_client import QdrantClient
    from carta.embed.embed import ensure_collection, collection_is_hybrid

    try:
        client = QdrantClient(url=qdrant_url, timeout=10)
    except Exception as e:
        print(f"  Error: could not connect to Qdrant at {qdrant_url}: {e}")
        return False

    failures = 0
    for type_ in ["doc", "session", "notes"]:
        collection = f"{project_name}_{type_}"
        try:
            existed = client.collection_exists(collection)
            ensure_collection(client, collection)
            if existed and not collection_is_hybrid(client, collection):
                print(
                    f"  Warning: existing collection {collection} uses a legacy "
                    f"non-hybrid schema — BM25+RRF hybrid retrieval is OFF for it. "
                    f"Drop it and re-embed to enable hybrid."
                )
        except Exception as e:
            print(f"  Error: could not create collection {collection}: {e}")
            failures += 1
    return failures == 0


def _update_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    entries = [
        ".carta/scan-results.json",
        ".carta/carta/",
        ".carta/hooks/",
        ".carta/visual_cache/",
        ".carta/update-check.json",
        # Belt and braces. Hook traces now live at ~/.carta/traces/<project>/
        # (carta/search/trace.py), outside every repo, precisely because they
        # contain the verbatim derived query text — but a project that ran an
        # earlier build may already have one of these on disk.
        ".carta/traces/",
    ]
    existing_lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    parent_globs = {".carta/", ".carta/*"}
    if parent_globs & set(existing_lines):
        return
    new_entries = [e for e in entries if e not in existing_lines]
    if not new_entries:
        return
    with open(gitignore, "a") as f:
        for entry in new_entries:
            f.write(f"\n{entry}")
        f.write("\n")


def _create_mcp_configs(project_root: Path) -> None:
    """Create MCP configuration files for non-Claude Code editors.

    Claude Code MCP registration (.mcp.json at plugin root) is now handled
    plugin-natively and must not be duplicated here to avoid conflicts for
    marketplace users.
    """
    import json

    # OpenCode: .opencode.json
    opencode_data = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "carta": {
                "type": "local",
                "command": ["carta-mcp"],
                "enabled": True
            }
        }
    }
    opencode_path = project_root / ".opencode.json"
    opencode_path.write_text(json.dumps(opencode_data, indent=2) + "\n")

    print(f"  MCP configs: {opencode_path}")


_CARTA_CLAUDE_SENTINEL = "<!-- carta:guidance:start -->"
_CARTA_CLAUDE_LEGACY = "Carta is active"


def _carta_claude_block(project_name: str) -> str:
    """The Carta /doc-search guidance block injected into a project's CLAUDE.md.

    Wrapped in carta:guidance markers for idempotent (re)writes; the trailing
    'Carta is active' comment is both a diagnostic breadcrumb and the legacy
    idempotency string."""
    return (
        "<!-- carta:guidance:start -->\n"
        "## Carta Knowledge Graph\n"
        "\n"
        "**Search the docs before you assume.** This project's specs (components, protocols,\n"
        "config keys, design decisions) may have changed since the code — or your training — was\n"
        "written. Carta provides semantic search over them via `/doc-search`.\n"
        "\n"
        "**Run `/doc-search` whenever a prompt names one of these — query it like so:**\n"
        "\n"
        "| Prompt mentions… | Search |\n"
        "|---|---|\n"
        "| A component or module | `/doc-search \"<name> responsibilities\"` |\n"
        "| An API, protocol, or data format | `/doc-search \"<name> spec\"` |\n"
        "| A config key or flag | `/doc-search \"<key> configuration\"` |\n"
        "| A file/path or a design decision | `/doc-search \"<topic> design\"` |\n"
        "\n"
        "Maintenance (only when asked, or after editing docs): `/doc-audit` (flag\n"
        "stale/contradictory docs), `/doc-embed` (re-index).\n"
        "\n"
        f"<!-- Carta is active. Collections: {project_name}_doc, "
        f"{project_name}_session, {project_name}_notes -->\n"
        "<!-- carta:guidance:end -->\n"
    )


def _append_claude_md(project_root: Path, project_name: str) -> None:
    """Inject the Carta guidance block into CLAUDE.md (create if absent, append if
    present). Idempotent: skips if the block marker or the legacy 'Carta is active'
    string is already present."""
    claude_md = project_root / "CLAUDE.md"
    block = _carta_claude_block(project_name)
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
        if _CARTA_CLAUDE_SENTINEL in text or _CARTA_CLAUDE_LEGACY in text:
            return
        with open(claude_md, "a", encoding="utf-8") as f:
            f.write("\n" + block)
    else:
        claude_md.write_text(f"# {project_name}\n\n{block}", encoding="utf-8")


def _create_agents_md(project_root: Path, project_name: str) -> None:
    """Create AGENTS.md with Carta slash commands for Claude Code."""
    agents_md = project_root / "AGENTS.md"
    if agents_md.exists():
        return  # Don't overwrite existing
    
    content = f'''# Carta Skills

This project uses [Carta](https://github.com/ian-q/carta) for semantic memory and document management.

## Slash Commands

### `/doc-audit`
Scan for documentation issues and contradictions.

**Example:**
```
/doc-audit
```

Runs a full audit and reports:
- Pending files needing embedding
- Drift detection (files changed since last audit)
- Missing references

Results saved to `.carta/scan-results.json`

---

### `/doc-embed`
Embed documents into the vector store for semantic search.

**Example:**
```
/doc-embed
```

Seeds the knowledge store by processing markdown/PDF files, generating embeddings, and upserting to Qdrant.

---

### `/doc-search <query>`
Search across embedded documents using natural language.

**Example:**
```
/doc-search how to configure the system
```

Returns top results from all collections with scores and excerpts.

---

### Saving project notes

Use the `carta_remember` MCP tool (or `carta remember "text" --type quirk`) to save durable
project knowledge — surprising quirks, bug-investigation findings, helpful notes. Notes are
written to docs/quirks/ or docs/notes/ (git-shareable) and are immediately searchable.

---

## Configuration

- **Project**: {project_name}
- **Qdrant**: http://localhost:6333
- **Ollama**: http://localhost:11434
- **Config**: `.carta/config.yaml`

## Quick Start

1. Check documentation health: `/doc-audit`
2. Seed knowledge store: `/doc-embed`
3. Search docs: `/doc-search <query>`

<!-- Carta is active. Collections: {project_name}_doc, {project_name}_session, {project_name}_notes -->
'''
    agents_md.write_text(content)
    print(f"  Created AGENTS.md with Carta slash commands")
