import shutil
import subprocess
import sys
from pathlib import Path
import yaml
import requests

CARTA_RUNTIME_SRC = Path(__file__).parent.parent / "carta"

def run_bootstrap(project_root: Path) -> None:
    project_name = _detect_project_name(project_root)
    print(f"Initialising Carta for project: {project_name}")

    qdrant_url = "http://localhost:6333"
    if not _check_qdrant(qdrant_url):
        print(f"  Qdrant not reachable at {qdrant_url}.")
        print("  Start it with: docker run -p 6333:6333 qdrant/qdrant")
        sys.exit(1)

    modules = {
        "doc_audit": True, "doc_embed": True, "doc_search": True,
        "session_memory": True, "proactive_recall": True,
    }

    _check_ollama("http://localhost:11434")

    carta_dir = project_root / ".carta"
    carta_dir.mkdir(exist_ok=True)
    _write_config(carta_dir, project_name, qdrant_url, modules)

    _register_hooks(project_root)
    _create_qdrant_collections(project_name, qdrant_url)
    _update_gitignore(project_root)

    runtime_dest = carta_dir / "carta"
    if runtime_dest.exists():
        shutil.rmtree(runtime_dest)
    shutil.copytree(CARTA_RUNTIME_SRC, runtime_dest)

    _append_claude_md(project_root, project_name)

    print(f"\nCarta ready. Collections: {project_name}:doc, {project_name}:session, {project_name}:quirk")
    print("Run /doc-embed to seed the knowledge store.")


def _detect_project_name(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=str(root)
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).name
    except FileNotFoundError:
        pass
    return root.name


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
    config = _deep_merge(DEFAULTS, {
        "project_name": project_name,
        "qdrant_url": qdrant_url,
        "modules": modules,
    })
    (carta_dir / "config.yaml").write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _register_hooks(project_root: Path) -> None:
    import json
    hooks_src = Path(__file__).parent.parent / "hooks"
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = str(hooks_src / "carta-prompt-hook.sh")
    hooks["Stop"] = str(hooks_src / "carta-stop-hook.sh")
    settings_path.write_text(json.dumps(settings, indent=2))


def _create_qdrant_collections(project_name: str, qdrant_url: str) -> None:
    for type_ in ["doc", "session", "quirk"]:
        collection = f"{project_name}:{type_}"
        try:
            requests.put(
                f"{qdrant_url}/collections/{collection}",
                json={"vectors": {"size": 768, "distance": "Cosine"}},
                timeout=5,
            )
        except Exception as e:
            print(f"  Warning: could not create collection {collection}: {e}")


def _update_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    entry = ".carta/scan-results.json"
    if gitignore.exists():
        if entry in gitignore.read_text():
            return
        with open(gitignore, "a") as f:
            f.write(f"\n{entry}\n")
    else:
        gitignore.write_text(f"{entry}\n")


def _append_claude_md(project_root: Path, project_name: str) -> None:
    claude_md = project_root / "CLAUDE.md"
    note = f"\n<!-- Carta is active. Collections: {project_name}:doc, {project_name}:session, {project_name}:quirk -->\n"
    if claude_md.exists():
        if "Carta is active" in claude_md.read_text():
            return
        with open(claude_md, "a") as f:
            f.write(note)
