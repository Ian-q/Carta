import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import requests
import yaml

CARTA_RUNTIME_SRC = Path(__file__).parent.parent

VECTOR_DIMENSIONS = {"doc": 768, "session": 768, "quirk": 768}


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


def run_bootstrap(project_root: Path) -> None:
    """Bootstrap Carta in a project with comprehensive preflight checks."""
    # Phase 0: Run comprehensive preflight checks
    from carta.install.preflight import PreflightChecker
    from carta.install.auto_fix import AutoInstaller

    interactive = _is_interactive()

    print("🔍 Running preflight checks...")
    checker = PreflightChecker(interactive=interactive, verbose=False)
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

    colls = f"{project_name}_doc, {project_name}_session, {project_name}_quirk"
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
            capture_output=True, text=True, cwd=str(root)
        )
        if result.returncode == 0:
            name = Path(result.stdout.strip()).name
            if name:
                return name
    except FileNotFoundError:
        pass
    name = root.name
    return name if name else "carta-project"


def _check_qdrant(url: str) -> bool:
    try:
        r = requests.get(f"{url}/healthz", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _check_ollama(url: str) -> bool:
    try:
        r = requests.get(url, timeout=3)
        return r.status_code == 200
    except Exception:
        print("  Warning: Ollama not reachable. Proactive recall will be skipped until Ollama is running.")
        return False


def _write_config(carta_dir: Path, project_name: str, qdrant_url: str, modules: dict) -> None:
    from carta.config import DEFAULTS, _deep_merge
    base = _deep_merge(DEFAULTS, {"modules": modules})
    # Hoist identity fields to the top for readability
    ordered = {
        "project_name": project_name,
        "qdrant_url": qdrant_url,
        **{k: v for k, v in base.items() if k not in ("project_name", "qdrant_url")},
    }
    (carta_dir / "config.yaml").write_text(yaml.dump(ordered, default_flow_style=False, sort_keys=False))


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
    """Create Qdrant collections. Returns True if all succeeded."""
    failures = 0
    for type_ in ["doc", "session", "quirk"]:
        collection = f"{project_name}_{type_}"
        try:
            r = requests.put(
                f"{qdrant_url}/collections/{collection}",
                json={"vectors": {"size": VECTOR_DIMENSIONS.get(type_, vector_size), "distance": "Cosine"}},
                timeout=5,
            )
            if r.status_code not in (200, 409):
                print(f"  Error: Qdrant returned {r.status_code} for collection {collection}: {r.text}")
                failures += 1
        except Exception as e:
            print(f"  Error: could not create collection {collection}: {e}")
            failures += 1
    return failures == 0


def _update_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    entries = [".carta/scan-results.json", ".carta/carta/", ".carta/hooks/"]
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


def _append_claude_md(project_root: Path, project_name: str) -> None:
    claude_md = project_root / "CLAUDE.md"
    note = f"\n<!-- Carta is active. Collections: {project_name}_doc, {project_name}_session, {project_name}_quirk -->\n"
    if claude_md.exists():
        if "Carta is active" in claude_md.read_text():
            return
        with open(claude_md, "a") as f:
            f.write(note)


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

### `/session-memory <text>`
Capture session context for future recall.

**Example:**
```
/session-memory save key decisions about API design
```

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

<!-- Carta is active. Collections: {project_name}_doc, {project_name}_session, {project_name}_quirk -->
'''
    agents_md.write_text(content)
    print(f"  Created AGENTS.md with Carta slash commands")
