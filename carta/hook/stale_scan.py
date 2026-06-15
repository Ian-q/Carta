"""Stale-reference scan: section changed docs, search the graph per section, and
ask a small Ollama judge whether any section has been superseded. Stage-agnostic
core (run_stale_scan) plus thin git collectors. Fails open everywhere."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from carta.scanner.scanner import is_excluded
from carta.embed.parse import chunk_text, sections_from_markdown


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


def run_stale_scan(repo_root, cfg, changed_docs, *, search_fn=None, judge_fn=None) -> StaleScanResult:
    """Scan changed_docs for superseded sections.

    search_fn(query) -> list of hit dicts {"source","score","excerpt"}.
    judge_fn(section_text, candidate_hit) -> True (stale) / False / None (unknown).
    Both default to the real search + Ollama stale judge; injectable for tests.
    """
    if search_fn is None:
        from carta.embed.pipeline import run_search
        search_fn = lambda q: run_search(q, _search_cfg(cfg))  # noqa: E731
    if judge_fn is None:
        judge_fn = lambda section_text, candidate: _stale_judge(section_text, candidate, cfg)  # noqa: E731

    sc = cfg.get("hooks", {}).get("stale_scan", {})
    threshold = sc.get("candidate_threshold", 0.65)
    max_judge_calls = sc.get("max_judge_calls", 30)
    max_tokens = cfg.get("embed", {}).get("chunking", {}).get("max_tokens", 400)

    result = StaleScanResult()
    for doc in changed_docs:
        result.scanned += 1
        sections, _ = sections_from_markdown(doc.text)
        chunks = chunk_text(sections, max_tokens=max_tokens)
        for chunk in chunks:
            try:
                hits = search_fn(chunk["text"])
            except Exception:
                continue  # fail open for this section
            hits = [h for h in (hits or []) if h.get("source") != doc.path]
            if not hits or hits[0].get("score", 0.0) < threshold:
                continue
            if result.judge_calls >= max_judge_calls:
                result.skipped_overflow += 1
                continue
            result.judge_calls += 1
            try:
                verdict = judge_fn(chunk["text"], hits[0])
            except Exception:
                verdict = None
            if verdict:
                result.findings.append(StaleFinding(
                    file=doc.path,
                    section=chunk.get("section_heading", ""),
                    snippet=chunk["text"][:160],
                    candidate_path=hits[0].get("source", ""),
                    candidate_score=hits[0].get("score", 0.0),
                ))
    return result
