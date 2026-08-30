"""Stale-reference scan: section changed docs, search the graph per section, and
ask a small Ollama judge whether any section has been superseded. Stage-agnostic
core (run_stale_scan) plus thin git collectors. Fails open everywhere."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from carta.scanner.scanner import is_excluded
from carta.embed.parse import chunk_text, sections_from_markdown
from carta.hook.judge import ollama_json


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
    candidate_excerpt: str = ""


@dataclass
class StaleScanResult:
    findings: list = field(default_factory=list)
    scanned: int = 0
    judge_calls: int = 0
    skipped_overflow: int = 0
    judge_errors: int = 0   # judge calls that returned None (timeout/error) — fail-open, but tracked
    # Candidates retrieved and fused but cut by run_search's top_n before the gate
    # could see them — a ceiling no threshold can recover from, and invisible
    # without counting, since a missed warning looks exactly like no warning.
    # The scan now searches to `candidate_depth` (30) rather than inheriting
    # search.top_n (5), so this should normally be 0; a non-zero value means even
    # 30 is too shallow for this corpus. 0 when search_fn is injected, since an
    # injected function carries no stage data to measure.
    candidates_truncated: int = 0


def _search_cfg(cfg: dict) -> dict:
    """Text-only search config: no ColPali, no rerank — the prompt-hook latency pattern.

    `top_n` is raised to `hooks.stale_scan.candidate_depth` (30). Without it the
    scan inherited `search.top_n` (5), and a superseding document fused at rank
    6-29 was retrieved, embedded and ranked — then truncated away before any
    threshold could see it. A gate cannot recover what the search already dropped,
    so the relevance floor alone would have been tuned against a ceiling (#121).
    Judge cost is unaffected: one call per chunk regardless of depth.
    """
    depth = cfg.get("hooks", {}).get("stale_scan", {}).get("candidate_depth", 30)
    return {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "top_n": depth,
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }


def _candidate_is_plausible(hit: dict, dense_floor: float) -> bool:
    """Is this candidate worth an Ollama judge call?

    A cheap cost throttle, NOT a precision instrument — the judge is the precision
    instrument (#84). Skip only on MEASURED irrelevance: a dense cosine below the
    floor. The asymmetry is load-bearing:

    * A hit with no `dense_score` — BM25-only on a hybrid collection — cannot be
      measurably irrelevant, so it is judged, never silenced. Dropping it is the
      recall bug this replaces, and supersession frequently turns on a shared
      identifier that only the sparse lane matches.
    * `score` is NOT consulted on the hybrid path. It is intra-collection RRF, a
      rank statistic whose scale depends on `rrf_k` and lane count; comparing it
      to a cosine threshold is the original defect.

    Deliberately NOT ported from the recall hook's `_gate_zone`: its lane-agreement
    clause. Agreement is evidence of topicality, which is what proactive recall
    wants. Supersession wants the CONTRADICTING document, which often agrees in one
    lane only.
    """
    dense = hit.get("dense_score")
    if dense is not None:
        return dense >= dense_floor
    if hit.get("lane_ranks"):
        return True                      # hybrid, sparse-only — nothing to measure
    score = hit.get("score")             # legacy non-hybrid collection: a real cosine
    return score is not None and score >= dense_floor


def _in_doc_scope(rel_path: str, cfg: dict, repo_root: Path) -> bool:
    """True if rel_path is a Markdown doc under docs_root and not excluded."""
    if not rel_path.endswith(".md"):
        return False
    docs_root = cfg.get("docs_root", "docs/").rstrip("/")
    if not (rel_path == docs_root or rel_path.startswith(docs_root + "/")):
        return False
    return not is_excluded(repo_root / rel_path, cfg, repo_root)


def _stale_judge(section_text: str, candidate: dict, cfg: dict):
    """Evidence-citation supersession gate. Returns True/False/None (None on error
    → caller fails open). True only when the judge cites a clause that genuinely
    conflicts with a claim in the section — not merely related/corroborating text."""
    sc = cfg.get("hooks", {}).get("stale_scan", {})
    ollama_url = cfg["embed"]["ollama_url"]
    model = sc.get("ollama_model", "qwen3.5:9b")
    timeout_s = sc.get("judge_timeout_s", 30)
    system = (
        "You decide whether a documentation section has been SUPERSEDED by a "
        "knowledge-base excerpt. A section is superseded ONLY if the excerpt states "
        "something that makes a specific claim in the section wrong, replaced, or "
        "deprecated. Content that is merely related, complementary, corroborating, or "
        "duplicated is NOT supersession. Respond with a JSON object only."
    )
    # 1500 (not 600): run_stale_scan already hands us a <=400-token chunk, and a
    # tighter cut here can sever the very claim the excerpt contradicts (the
    # contradicted clause often sits after the matching prose).
    user = (
        f"Committed section:\n{section_text[:1500]}\n\n"
        f"Knowledge-base excerpt ({candidate.get('source', '')}):\n"
        f"{candidate.get('excerpt', '')[:1500]}\n\n"
        'Return a JSON object with exactly these keys: '
        '"section_claim" (an exact quote from the committed section), '
        '"doc_clause" (an exact quote from the excerpt), and '
        '"conflict" (true or false). '
        'Set "conflict" to true ONLY if doc_clause makes section_claim wrong, replaced, '
        'or deprecated. If the excerpt merely repeats, supports, or relates to the '
        'section, set "conflict" to false.'
    )
    result = ollama_json(ollama_url, model, system, user, timeout_s=timeout_s)
    if not isinstance(result, dict) or "conflict" not in result:
        return None
    return bool(result["conflict"])


ZERO_OID = "0" * 40
# git's canonical empty-tree object — diffing EMPTY_TREE..<sha> lists every file at <sha>
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout


def _default_branch(repo_root: Path) -> str:
    try:
        ref = _git(repo_root, "rev-parse", "--abbrev-ref", "origin/HEAD").strip()
        return ref.split("/", 1)[1] if "/" in ref else ref
    except subprocess.CalledProcessError:
        return "main"


def _new_branch_range(repo_root: Path, local_sha: str) -> str:
    """Range spec for a brand-new branch (remote has no tracking ref).

    Prefer commits since the fork point off the default branch; when there is no
    usable merge base (unrelated history, default branch absent, or the tip is
    already contained in the default branch) fall back to the whole history at the
    tip via git's empty-tree object so a first push still surfaces its docs."""
    base = _default_branch(repo_root)
    try:
        mb = _git(repo_root, "merge-base", base, local_sha).strip()
    except subprocess.CalledProcessError:
        mb = ""
    if mb and mb != local_sha:
        return f"{mb}..{local_sha}"
    return f"{EMPTY_TREE}..{local_sha}"


def collect_staged(repo_root: Path, cfg: dict) -> list[ChangedDoc]:
    out = _git(repo_root, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    docs: list[ChangedDoc] = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel or not _in_doc_scope(rel, cfg, repo_root):
            continue
        try:
            text = _git(repo_root, "show", f":{rel}")
        except subprocess.CalledProcessError:
            continue
        docs.append(ChangedDoc(path=rel, text=text))
    return docs


def collect_pushed(repo_root: Path, cfg: dict, stdin_lines: list[str]) -> list[ChangedDoc]:
    ranges: list[tuple[str, str]] = []  # (range_spec, tip_sha)
    for line in stdin_lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if set(local_sha) == {"0"}:
            continue  # branch deletion — nothing to scan
        if set(remote_sha) == {"0"}:
            rng = _new_branch_range(repo_root, local_sha)
        else:
            rng = f"{remote_sha}..{local_sha}"
        ranges.append((rng, local_sha))

    if not stdin_lines:  # manual invocation — scan default-branch..HEAD
        ranges = [(f"{_default_branch(repo_root)}..HEAD", "HEAD")]

    return _collect_from_ranges(repo_root, cfg, ranges)


def _collect_from_ranges(
    repo_root: Path, cfg: dict, ranges: list[tuple[str, str]]
) -> list[ChangedDoc]:
    """Collect in-scope changed docs across one or more (range_spec, tip_sha) pairs.

    For each range, list ACM-changed paths, keep only in-scope docs, and read each
    doc's content at that range's tip via `git show <tip>:<path>`. Deduped by path
    (first range wins). Fails open per range and per file."""
    seen: dict[str, ChangedDoc] = {}
    for rng, tip in ranges:
        try:
            out = _git(repo_root, "diff", "--name-only", "--diff-filter=ACM", rng)
        except subprocess.CalledProcessError:
            continue
        for rel in out.splitlines():
            rel = rel.strip()
            if not rel or rel in seen or not _in_doc_scope(rel, cfg, repo_root):
                continue
            try:
                text = _git(repo_root, "show", f"{tip}:{rel}")
            except subprocess.CalledProcessError:
                continue
            seen[rel] = ChangedDoc(path=rel, text=text)
    return list(seen.values())


def _range_tip(range_spec: str) -> str:
    """Right operand of a git range (`A..B` / `A...B` -> `B`); empty right side or a
    bare ref -> `HEAD`. Used to read changed-file content at the tip of the range."""
    for sep in ("...", ".."):
        if sep in range_spec:
            right = range_spec.split(sep, 1)[1].strip()
            return right or "HEAD"
    return range_spec.strip() or "HEAD"


def collect_range(repo_root: Path, cfg: dict, range_spec: str) -> list[ChangedDoc]:
    """Collect in-scope docs changed across an explicit git range, read at the range
    tip. Used by the local on-demand pre-PR diff scan (`carta hook check --diff`)."""
    return _collect_from_ranges(repo_root, cfg, [(range_spec, _range_tip(range_spec))])


def run_stale_scan(repo_root, cfg, changed_docs, *, search_fn=None, judge_fn=None) -> StaleScanResult:
    """Scan changed_docs for superseded sections.

    search_fn(query) -> list of hit dicts {"source","score","excerpt"}.
    judge_fn(section_text, candidate_hit) -> True (stale) / False / None (unknown).
    Both default to the real search + Ollama stale judge; injectable for tests.
    """
    # Candidates fused but cut by top_n before the gate, accumulated across every
    # chunk searched. Only the default search_fn can measure this — it is the only
    # one with access to run_search's stage snapshots.
    truncated: list[int] = []
    if search_fn is None:
        from carta.embed.pipeline import run_search

        def search_fn(q):  # noqa: E731 - closure over `truncated` by design
            stages: dict = {}
            hits = run_search(q, _search_cfg(cfg), trace_stages=stages)
            pool = stages.get("post_dedupe") or stages.get("fused") or []
            truncated.append(max(0, len(pool) - len(hits)))
            return hits
    if judge_fn is None:
        judge_fn = lambda section_text, candidate: _stale_judge(section_text, candidate, cfg)  # noqa: E731

    sc = cfg.get("hooks", {}).get("stale_scan", {})
    # `candidate_threshold` is deliberately NOT read: it gated fused RRF output as
    # though it were a cosine (#121). See config.py for the deprecation note.
    dense_floor = sc.get("candidate_dense_threshold", 0.55)
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
            # Take the best PLAUSIBLE candidate in fused order rather than gating
            # on hits[0] alone. hits[0] is the cross-collection argmax by
            # `fused_score`, but `score` is intra-collection — so gating on hits[0]
            # discarded better candidates sitting right behind it. Fused order is
            # the retrieval system's own ranking and is kept; this only skips past
            # candidates measured irrelevant. Still one judge call per chunk.
            candidate = next(
                (h for h in hits if _candidate_is_plausible(h, dense_floor)), None
            )
            if candidate is None:
                continue
            if result.judge_calls >= max_judge_calls:
                result.skipped_overflow += 1
                continue
            result.judge_calls += 1
            try:
                verdict = judge_fn(chunk["text"], candidate)
            except Exception:
                verdict = None
            if verdict is None:
                result.judge_errors += 1
            if verdict:
                result.findings.append(StaleFinding(
                    file=doc.path,
                    section=chunk.get("section_heading", ""),
                    snippet=chunk["text"][:160],
                    candidate_path=candidate.get("source", ""),
                    candidate_score=candidate.get("score") or 0.0,
                    candidate_excerpt=candidate.get("excerpt", ""),
                ))
    result.candidates_truncated = sum(truncated)
    return result
