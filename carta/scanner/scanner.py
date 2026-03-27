"""Carta documentation structural scanner."""

import fnmatch
import json
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(doc_path: Path) -> Optional[dict]:
    """Return parsed YAML frontmatter dict, or None if none present."""
    text = doc_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    yaml_block = text[3:end].strip()
    try:
        data = yaml.safe_load(yaml_block)
        if not isinstance(data, dict):
            return None
        # Normalize date objects to ISO strings so callers always get strings
        for key, value in data.items():
            if isinstance(value, (date, datetime)):
                data[key] = value.isoformat()
        return data
    except yaml.YAMLError:
        return None


# ---------------------------------------------------------------------------
# Exclusion helpers
# ---------------------------------------------------------------------------

def is_excluded(file_path: Path, cfg: dict, repo_root: Path) -> bool:
    """Return True if file_path matches any excluded_paths pattern in cfg."""
    try:
        rel = str(file_path.relative_to(repo_root))
    except ValueError:
        rel = str(file_path)

    for pattern in cfg.get("excluded_paths", []):
        if pattern.endswith("/"):
            if rel.startswith(pattern) or ("/" + pattern.rstrip("/") + "/" in "/" + rel):
                return True
        elif fnmatch.fnmatch(file_path.name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------------------

def _iter_md_files(repo_root: Path):
    """Yield all .md files under repo_root, skipping .git."""
    for p in repo_root.rglob("*.md"):
        if ".git" in p.parts:
            continue
        yield p


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

# Standard root-level markdown / convention files — never flagged as homeless_doc.
DEFAULT_HOMELESS_ROOT_WHITELIST = frozenset({
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    "CODEOWNERS",
})


def _anchor_basenames(cfg: dict) -> set[str]:
    """Basenames for anchor_doc / anchor_docs (paths like ./CLAUDE.md → CLAUDE.md)."""
    names: set[str] = set()
    anchor_doc = cfg.get("anchor_doc")
    if anchor_doc:
        names.add(Path(anchor_doc).name)
    for item in cfg.get("anchor_docs", []) or []:
        names.add(Path(item).name)
    return names


def check_homeless_docs(repo_root: Path, cfg: dict) -> list:
    """Flag .md files outside docs/ that aren't README.md, anchor_doc, or excluded."""
    issues = []
    docs_root = repo_root / cfg.get("docs_root", "docs/").rstrip("/")
    anchor_names = _anchor_basenames(cfg)
    for p in _iter_md_files(repo_root):
        if p.name == "README.md":
            continue
        if p.name in DEFAULT_HOMELESS_ROOT_WHITELIST:
            continue
        if p.name in anchor_names:
            continue
        if is_excluded(p, cfg, repo_root):
            continue
        try:
            p.relative_to(docs_root)
            continue  # inside docs/ — fine
        except ValueError:
            pass
        rel = str(p.relative_to(repo_root))
        issues.append({
            "type": "homeless_doc",
            "severity": "warning",
            "doc": rel,
            "detail": f"{rel} is outside docs/ and is not a README",
        })
    return issues


def check_broken_related(doc_path: Path, frontmatter: dict, repo_root: Path) -> list:
    """Flag related: entries that don't resolve to real files."""
    issues = []
    for rel_path in frontmatter.get("related") or []:
        target = repo_root / rel_path
        if not target.exists():
            rel_doc = str(doc_path.relative_to(repo_root))
            issues.append({
                "type": "broken_related",
                "severity": "error",
                "doc": rel_doc,
                "detail": f"related: entry '{rel_path}' does not exist",
                "related_file": rel_path,
            })
    return issues


def check_missing_frontmatter(doc_path: Path, frontmatter) -> Optional[dict]:
    """Return an issue if a tracked doc has no frontmatter."""
    if frontmatter is None:
        return {
            "type": "missing_frontmatter",
            "severity": "info",
            "doc": str(doc_path),
            "detail": "No YAML frontmatter block found",
        }
    return None


def check_prototype_doc(doc_path: Path, frontmatter: dict, repo_root: Path) -> Optional[dict]:
    """Return an info issue for docs with status: prototype."""
    if frontmatter.get("status") == "prototype":
        return {
            "type": "prototype_doc",
            "severity": "info",
            "doc": str(doc_path.relative_to(repo_root)),
            "detail": "Prototype-scoped doc — set status: archived when no longer current.",
        }
    return None


# Statuses that skip time-based checks — doc is intentionally static
_STATIC_STATUSES = frozenset(["prototype", "archived"])


def check_stale_last_reviewed(
    doc_path: Path, frontmatter: dict, threshold_days: int, reference_date: date
) -> list:
    """Flag docs where last_reviewed is older than threshold_days."""
    if frontmatter.get("status") in _STATIC_STATUSES:
        return []
    lr = frontmatter.get("last_reviewed")
    if not lr:
        return []
    if isinstance(lr, str):
        try:
            reviewed = date.fromisoformat(lr)
        except ValueError:
            return []
    elif isinstance(lr, date):
        reviewed = lr
    else:
        return []

    delta = (reference_date - reviewed).days
    if delta > threshold_days:
        return [{
            "type": "stale_last_reviewed",
            "severity": "warning",
            "doc": str(doc_path),
            "detail": f"last_reviewed {lr} is {delta} days ago (threshold: {threshold_days})",
        }]
    return []


def get_file_last_commit_date(repo_root: Path, file_path: Path) -> Optional[date]:
    """Return the date of the most recent git commit touching file_path, or None."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%as", "--", str(file_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    try:
        return date.fromisoformat(stdout)
    except ValueError:
        return None


def check_related_drift(doc_path: Path, frontmatter: dict, repo_root: Path) -> list:
    """Flag when a related: file was git-committed after this doc's last_reviewed."""
    if frontmatter.get("status") in _STATIC_STATUSES:
        return []
    lr = frontmatter.get("last_reviewed")
    if not lr:
        return []
    if isinstance(lr, str):
        try:
            reviewed = date.fromisoformat(str(lr))
        except ValueError:
            return []
    elif isinstance(lr, date):
        reviewed = lr
    else:
        return []

    issues = []
    rel_doc = str(doc_path.relative_to(repo_root))
    for rel_path in frontmatter.get("related") or []:
        last_commit = get_file_last_commit_date(repo_root, Path(rel_path))
        if last_commit and last_commit > reviewed:
            issues.append({
                "type": "stale_related",
                "severity": "warning",
                "doc": rel_doc,
                "detail": f"{rel_path} modified {last_commit} but last_reviewed {lr}",
                "related_file": rel_path,
                "related_git_hash": None,
            })
    return issues


def build_inverted_index(docs_with_frontmatter: dict) -> dict:
    """Build inverted index: related_path -> set of docs that list it in related:."""
    idx: dict = {}
    for doc_path, fm in docs_with_frontmatter.items():
        if not fm:
            continue
        for rel in fm.get("related") or []:
            idx.setdefault(rel, set()).add(doc_path)
    return idx


def check_orphaned_doc(
    doc_path: Path, frontmatter, inverted_index: dict, repo_root: Path
) -> Optional[dict]:
    """Flag docs not referenced by any other doc's related: AND with no folder siblings."""
    rel = str(doc_path.relative_to(repo_root))
    if rel in inverted_index:
        return None
    siblings = [
        p for p in doc_path.parent.iterdir()
        if p.is_file() and p.suffix == ".md" and p != doc_path
    ]
    if siblings:
        return None
    return {
        "type": "orphaned_doc",
        "severity": "info",
        "doc": rel,
        "detail": f"{rel} has no inbound related: links and no folder siblings",
    }


def check_nested_docs_folders(repo_root: Path, cfg: dict = None) -> list:
    """Flag any */docs/ directory that isn't the root docs/."""
    cfg = cfg or {}
    issues = []
    docs_root = repo_root / cfg.get("docs_root", "docs/").rstrip("/")
    for p in repo_root.rglob("docs"):
        if not p.is_dir():
            continue
        if ".git" in p.parts:
            continue
        if p == docs_root:
            continue
        nested_dir = p
        rel = str(nested_dir.relative_to(repo_root))
        if is_excluded(nested_dir, cfg, repo_root):
            continue
        issues.append({
            "type": "nested_docs_folder",
            "severity": "warning",
            "doc": rel,
            "detail": f"Subdirectory {rel} is a docs/ folder — consolidate into root docs/",
        })
    return issues


# ---------------------------------------------------------------------------
# Sidecar (.embed-meta.yaml) support
# ---------------------------------------------------------------------------

_SIDECAR_SKIP_DIRS = frozenset([".git", ".pio", "node_modules", "build", "install", "__pycache__"])


def _iter_sidecar_files(repo_root: Path, cfg: dict):
    """Yield all .embed-meta.yaml files under repo_root, skipping build-artifact dirs."""
    for p in repo_root.rglob("*.embed-meta.yaml"):
        if any(part in _SIDECAR_SKIP_DIRS for part in p.parts):
            continue
        yield p


def parse_sidecar(sidecar_path: Path) -> Optional[dict]:
    """Return parsed sidecar YAML dict, or None on error."""
    try:
        data = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (yaml.YAMLError, OSError):
        return None


def check_sidecar_path_drift(
    sidecar_path: Path, sidecar_data: dict, repo_root: Path
) -> Optional[dict]:
    """Flag if current_path in sidecar is missing or points to a non-existent file."""
    rel_sidecar = str(sidecar_path.relative_to(repo_root))
    current_path = sidecar_data.get("current_path")
    if not current_path:
        return {
            "type": "sidecar_missing_current_path",
            "severity": "info",
            "doc": rel_sidecar,
            "detail": "sidecar has no current_path field — cannot verify target file exists",
        }
    target = repo_root / current_path
    if not target.exists():
        return {
            "type": "sidecar_path_drift",
            "severity": "warning",
            "doc": rel_sidecar,
            "detail": f"current_path '{current_path}' does not exist — file may have moved or been deleted",
            "expected_path": current_path,
        }
    return None


def check_sidecar_broken_related(
    sidecar_path: Path, sidecar_data: dict, repo_root: Path
) -> list:
    """Flag related_docs entries in sidecar that don't resolve to real files."""
    issues = []
    rel_sidecar = str(sidecar_path.relative_to(repo_root))
    for rel_path in sidecar_data.get("related_docs") or []:
        target = repo_root / rel_path
        if not target.exists():
            issues.append({
                "type": "sidecar_broken_related",
                "severity": "warning",
                "doc": rel_sidecar,
                "detail": f"related_docs entry '{rel_path}' does not exist",
                "related_file": rel_path,
            })
    return issues


# ---------------------------------------------------------------------------
# Embed file type checks (config-driven scan dirs)
# ---------------------------------------------------------------------------

_EMBED_EXTENSIONS = frozenset([".pdf", ".m4a", ".mp3", ".wav", ".aac"])


def _get_embed_scan_dirs(cfg: dict) -> list:
    """Return embed scan dirs from config, falling back to standard locations."""
    embed_cfg = cfg.get("embed", {})
    dirs = []
    ref = embed_cfg.get("reference_docs_path", "docs/reference/").rstrip("/")
    audio = embed_cfg.get("audio_path", "docs/audio/").rstrip("/")
    dirs.append(ref)
    dirs.append(f"{audio}/inputs")
    return dirs


def check_embed_induction_needed(repo_root: Path, cfg: dict = None) -> list:
    """Flag embeddable files (PDFs, audio) that have no sidecar or status: pending."""
    issues = []
    scan_dirs = _get_embed_scan_dirs(cfg or {})
    for scan_dir in scan_dirs:
        dir_path = repo_root / scan_dir
        if not dir_path.exists():
            continue
        for f in dir_path.rglob("*"):
            if f.suffix.lower() not in _EMBED_EXTENSIONS:
                continue
            if f.name.startswith("."):
                continue
            rel = str(f.relative_to(repo_root))
            sidecar = f.parent / (f.stem + ".embed-meta.yaml")
            if not sidecar.exists():
                issues.append({
                    "type": "embed_induction_needed",
                    "severity": "warning",
                    "doc": rel,
                    "detail": "Embeddable file has no .embed-meta.yaml sidecar.",
                })
            else:
                data = parse_sidecar(sidecar)
                if data and data.get("status") == "pending":
                    issues.append({
                        "type": "embed_induction_needed",
                        "severity": "warning",
                        "doc": rel,
                        "detail": "Sidecar exists but status is 'pending' — not yet embedded.",
                    })
    return issues


def check_embed_drift(repo_root: Path, cfg: dict = None) -> list:
    """Flag embedded files whose mtime is newer than the sidecar's file_mtime.

    Only checks sidecars with status='embedded' and a file_mtime field.
    Returns list of issue dicts with type='embed_drift'.
    """
    import os
    issues = []
    scan_dirs = _get_embed_scan_dirs(cfg or {})
    for scan_dir in scan_dirs:
        dir_path = repo_root / scan_dir
        if not dir_path.exists():
            continue
        for f in dir_path.rglob("*"):
            if f.suffix.lower() not in _EMBED_EXTENSIONS:
                continue
            if f.name.startswith("."):
                continue
            sidecar = f.parent / (f.stem + ".embed-meta.yaml")
            if not sidecar.exists():
                continue  # no sidecar = pending, not drift
            data = parse_sidecar(sidecar)
            if not data:
                continue
            if data.get("status") != "embedded":
                continue  # only check embedded files
            stored_mtime = data.get("file_mtime")
            if stored_mtime is None:
                continue  # legacy sidecar without mtime
            try:
                current_mtime = os.path.getmtime(str(f))
            except OSError:
                continue
            if current_mtime > stored_mtime:
                rel = str(f.relative_to(repo_root))
                issues.append({
                    "type": "embed_drift",
                    "severity": "warning",
                    "doc": rel,
                    "detail": f"File modified since last embed (mtime {current_mtime:.0f} > stored {stored_mtime:.0f})",
                })
    return issues


def check_embed_lfs_not_pulled(repo_root: Path, cfg: dict = None) -> list:
    """Flag embeddable files that are Git LFS pointers (content not pulled)."""
    issues = []
    scan_dirs = _get_embed_scan_dirs(cfg or {})
    for scan_dir in scan_dirs:
        dir_path = repo_root / scan_dir
        if not dir_path.exists():
            continue
        for f in dir_path.rglob("*"):
            if f.suffix.lower() not in _EMBED_EXTENSIONS:
                continue
            if f.name.startswith("."):
                continue
            try:
                head = f.read_bytes()[:128]
                if head.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    rel = str(f.relative_to(repo_root))
                    issues.append({
                        "type": "embed_lfs_not_pulled",
                        "severity": "info",
                        "doc": rel,
                        "detail": "File is a Git LFS pointer — pull content before embedding.",
                    })
            except OSError:
                pass
    return issues


def check_embed_transcript_unprocessed(repo_root: Path, cfg: dict = None) -> list:
    """Flag audio files that have a transcript but no processed summary."""
    issues = []
    audio_base = (cfg or {}).get("embed", {}).get("audio_path", "docs/audio/").rstrip("/")
    transcripts_dir = repo_root / audio_base / "transcripts"
    processed_dir = repo_root / audio_base / "processed"
    inputs_dir = repo_root / audio_base / "inputs"

    if not transcripts_dir.exists() or not inputs_dir.exists():
        return issues

    for transcript in transcripts_dir.glob("*.txt"):
        stem = transcript.stem
        summary = processed_dir / f"{stem}-summary.md" if processed_dir.exists() else None
        if summary and summary.exists():
            continue

        sidecar = inputs_dir / f"{stem}.embed-meta.yaml"
        if sidecar.exists():
            data = parse_sidecar(sidecar)
            if data and data.get("status") in ("integrated", "fulfilled"):
                continue

        rel = str(transcript.relative_to(repo_root))
        issues.append({
            "type": "embed_transcript_unprocessed",
            "severity": "warning",
            "doc": rel,
            "detail": "Transcript exists but no processed summary found.",
        })

    return issues


# ---------------------------------------------------------------------------
# Git hash utilities
# ---------------------------------------------------------------------------

def get_current_git_hash(repo_root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def get_changed_since_hash(repo_root: Path, previous_hash: str, cfg: dict) -> list[str]:
    """Return .md and .embed-meta.yaml files changed since previous_hash, excluding excluded_paths."""
    changed: list[str] = []
    for pattern in ("*.md", "*.embed-meta.yaml"):
        result = subprocess.run(
            ["git", "diff", "--name-only", previous_hash, "HEAD", "--", pattern],
            cwd=repo_root, capture_output=True, text=True,
        )
        if result.returncode == 0:
            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
            changed.extend(f for f in files if not is_excluded(repo_root / f, cfg, repo_root))
    return changed


def get_initial_commit_hash(repo_root: Path) -> Optional[str]:
    """Return the hash of the very first commit in the repo, or None."""
    result = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0].strip()


def _load_previous_scan(scan_output_path: Path) -> Optional[dict]:
    if scan_output_path.exists():
        try:
            return json.loads(scan_output_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_scan(
    repo_root: Path,
    cfg: dict,
    output_path: Optional[Path] = None,
    reference_date: Optional[date] = None,
    verbose: bool = False,
) -> dict:
    """Run all structural checks and return the results dict.

    Args:
        repo_root: Root of the repository to scan.
        cfg: Config dict (from carta.config.load_config or a minimal dict for tests).
        output_path: Where to write scan-results.json. If None, written to repo_root/scan-results.json.
        reference_date: Override today's date for stale checks (useful in tests).

    Returns:
        JSON-serialisable dict with keys: scan_time, issues, stats, ...
    """
    ref_date = reference_date or date.today()

    effective_output = output_path if output_path is not None else repo_root / "scan-results.json"

    # Load previous scan for baseline hash
    previous_scan = _load_previous_scan(effective_output)
    previous_hash = previous_scan.get("run_at_git_hash") if previous_scan else None

    current_hash = get_current_git_hash(repo_root) or ""
    git_branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root, capture_output=True, text=True
    )
    git_branch = git_branch_result.stdout.strip()

    # Collect all tracked docs (inside docs_root, not excluded)
    docs_root_rel = cfg.get("docs_root", "docs/").rstrip("/")
    docs_root = repo_root / docs_root_rel
    tracked_docs: list[Path] = []
    if docs_root.exists():
        for p in docs_root.rglob("*.md"):
            if not is_excluded(p, cfg, repo_root):
                tracked_docs.append(p)

    # Parse frontmatter for all tracked docs
    frontmatters = {str(p.relative_to(repo_root)): parse_frontmatter(p) for p in tracked_docs}

    # Build inverted index
    inverted_index = build_inverted_index(frontmatters)

    # Changed since last audit
    if previous_hash:
        changed = get_changed_since_hash(repo_root, previous_hash, cfg)
    else:
        # First run: list all .md / .embed-meta.yaml files tracked by git so that
        # changed_since_last_audit covers the entire repo (not just docs_root).
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root, capture_output=True, text=True,
        )
        if result.returncode == 0:
            all_tracked = [f.strip() for f in result.stdout.splitlines() if f.strip()]
            changed = [
                f for f in all_tracked
                if (f.endswith(".md") or f.endswith(".embed-meta.yaml"))
                and not is_excluded(repo_root / f, cfg, repo_root)
            ]
        else:
            # Not a git repo — fall back to tracked_docs inside docs_root
            changed = [str(p.relative_to(repo_root)) for p in tracked_docs]

    # Run all checks
    issues: list[dict] = []
    issues.extend(check_homeless_docs(repo_root, cfg))
    issues.extend(check_nested_docs_folders(repo_root, cfg))

    threshold = cfg.get("stale_threshold_days", 30)
    for doc_path in tracked_docs:
        rel = str(doc_path.relative_to(repo_root))
        fm = frontmatters.get(rel)
        if fm is None:
            issue = check_missing_frontmatter(doc_path, fm)
            if issue:
                issue["doc"] = rel
                issues.append(issue)
            continue
        prototype = check_prototype_doc(doc_path, fm, repo_root)
        if prototype:
            issues.append(prototype)
        issues.extend(check_broken_related(doc_path, fm, repo_root))
        issues.extend(check_stale_last_reviewed(doc_path, fm, threshold, ref_date))
        issues.extend(check_related_drift(doc_path, fm, repo_root))
        orphan = check_orphaned_doc(doc_path, fm, inverted_index, repo_root)
        if orphan:
            issues.append(orphan)

    # Embed file type checks
    issues.extend(check_embed_induction_needed(repo_root, cfg))
    issues.extend(check_embed_lfs_not_pulled(repo_root, cfg))
    issues.extend(check_embed_transcript_unprocessed(repo_root, cfg))

    # Sidecar checks
    sidecar_files = list(_iter_sidecar_files(repo_root, cfg))
    for sidecar_path in sidecar_files:
        data = parse_sidecar(sidecar_path)
        if data is None:
            rel = str(sidecar_path.relative_to(repo_root))
            issues.append({
                "type": "sidecar_parse_error",
                "severity": "error",
                "doc": rel,
                "detail": "Failed to parse sidecar YAML — check for syntax errors",
            })
            continue
        drift = check_sidecar_path_drift(sidecar_path, data, repo_root)
        if drift:
            issues.append(drift)
        issues.extend(check_sidecar_broken_related(sidecar_path, data, repo_root))

    # Build stats
    by_severity = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        sev = i.get("severity", "info")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    now = datetime.now()
    result = {
        "scan_time": now.isoformat(timespec="seconds"),
        # Legacy alias kept for compatibility
        "run_at": now.isoformat(timespec="seconds"),
        "run_at_git_hash": current_hash,
        "git_branch": git_branch,
        "issues": issues,
        "changed_since_last_audit": changed,
        "stats": {
            "docs_scanned": len(tracked_docs),
            "with_frontmatter": sum(1 for fm in frontmatters.values() if fm is not None),
            "with_id": sum(1 for fm in frontmatters.values() if fm and fm.get("id")),
            "sidecars": len(sidecar_files),
            "homeless": sum(1 for i in issues if i["type"] == "homeless_doc"),
            "by_status": {
                s: sum(1 for fm in frontmatters.values() if fm and fm.get("status") == s)
                for s in ("active", "prototype", "archived")
            },
            "issues_by_severity": by_severity,
        },
    }

    # Write output
    effective_output.parent.mkdir(parents=True, exist_ok=True)
    effective_output.write_text(json.dumps(result, indent=2, default=str))

    return result
