import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
import yaml
import requests

CARTA_RUNTIME_SRC = Path(__file__).parent.parent

VECTOR_DIMENSIONS = {"doc": 768, "session": 768, "quirk": 768}

def run_bootstrap(project_root: Path) -> None:
    project_name = _detect_project_name(project_root)
    print(f"Initialising Carta for project: {project_name}")

    qdrant_url = os.environ.get("CARTA_QDRANT_URL", "http://localhost:6333")
    if not _check_qdrant(qdrant_url):
        print(f"  Qdrant not reachable at {qdrant_url}.")
        print("  Start it with: docker run -p 6333:6333 qdrant/qdrant")
        sys.exit(1)
    print(f"  Qdrant ready at {qdrant_url}")

    modules = {
        "doc_audit": True, "doc_embed": True, "doc_search": True,
        "session_memory": True, "proactive_recall": True,
    }

    ollama_url = os.environ.get("CARTA_OLLAMA_URL", "http://localhost:11434")
    if _check_ollama(ollama_url):
        print(f"  Ollama ready at {ollama_url}")
    # _check_ollama already prints a warning on failure — no else needed

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

    colls = f"{project_name}_doc, {project_name}_session, {project_name}_quirk"
    if collections_ok:
        print(f"\nCarta ready. Collections: {colls}")
        print("Run /doc-embed to seed the knowledge store.")
    else:
        print(f"\nCarta initialised but Qdrant collections could not be created.")
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
    import json
    hooks_src = Path(__file__).parent.parent / "hooks"
    hooks_dest = project_root / ".carta" / "hooks"
    hooks_dest.mkdir(parents=True, exist_ok=True)
    for script in hooks_src.glob("*.sh"):
        dest_script = hooks_dest / script.name
        shutil.copy2(script, dest_script)
        dest_script.chmod(dest_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, ValueError):
            print("  Warning: .claude/settings.json is malformed — recreating it.")
            settings = {}
    else:
        settings = {}
    hooks = settings.setdefault("hooks", {})
    hook_scripts = {
        "UserPromptSubmit": "carta-prompt-hook.sh",
        "Stop": "carta-stop-hook.sh",
    }
    for hook_name, script_name in hook_scripts.items():
        existing = hooks.get(hook_name)
        if existing and "carta" not in str(existing).lower():
            print(f"  Warning: overwriting existing {hook_name} hook: {existing}")
        cmd = f"bash -c 'exec \"$(git rev-parse --show-toplevel)/.carta/hooks/{script_name}\"'"
        hooks[hook_name] = [{"matcher": "", "hooks": [{"type": "command", "command": cmd}]}]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


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
    """Create MCP configuration files for both Claude Code and OpenCode."""
    import json
    
    # Claude Code: .mcp.json
    mcp_data = {
        "mcpServers": {
            "carta": {
                "command": "carta-mcp",
                "args": [],
                "env": {}
            }
        }
    }
    mcp_path = project_root / ".mcp.json"
    mcp_path.write_text(json.dumps(mcp_data, indent=2) + "\n")
    
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
    
    print(f"  MCP configs: {mcp_path}, {opencode_path}")


def _append_claude_md(project_root: Path, project_name: str) -> None:
    claude_md = project_root / "CLAUDE.md"
    note = f"\n<!-- Carta is active. Collections: {project_name}_doc, {project_name}_session, {project_name}_quirk -->\n"
    if claude_md.exists():
        if "Carta is active" in claude_md.read_text():
            return
        with open(claude_md, "a") as f:
            f.write(note)
