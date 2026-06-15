"""Stale-reference scan: section changed docs, search the graph per section, and
ask a small Ollama judge whether any section has been superseded. Stage-agnostic
core (run_stale_scan) plus thin git collectors. Fails open everywhere."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from carta.scanner.scanner import is_excluded


@dataclass
class ChangedDoc:
    path: str   # repo-relative doc path
    text: str   # changed/staged content to scan


@dataclass
class StaleFinding:
    file: str
    section: str
    snippet: str
    candidate_path: str
    candidate_score: float


@dataclass
class StaleScanResult:
    findings: list = field(default_factory=list)
    scanned: int = 0
    judge_calls: int = 0
    skipped_overflow: int = 0


def _search_cfg(cfg: dict) -> dict:
    """Text-only search config: no ColPali, no rerank — the prompt-hook latency pattern."""
    return {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }


def _in_doc_scope(rel_path: str, cfg: dict, repo_root: Path) -> bool:
    """True if rel_path is a Markdown doc under docs_root and not excluded."""
    if not rel_path.endswith(".md"):
        return False
    docs_root = cfg.get("docs_root", "docs/").rstrip("/")
    if not (rel_path == docs_root or rel_path.startswith(docs_root + "/")):
        return False
    return not is_excluded(repo_root / rel_path, cfg, repo_root)
