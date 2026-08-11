"""Retrieval tracing: what each stage did with each result.

A retrieval miss can happen at five stages (never retrieved, one lane only,
demoted by fusion, collapsed by dedup, dropped by the visual cap) and they are
indistinguishable from the outside. This records which one.

Two consumers: the recall hook appends JSONL for gate calibration (to
`~/.carta/traces/<project>/`, machine-level state outside every repo — see
`_trace_path`); `carta search --trace` prints per-stage ranks for error
analysis.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Anything outside this set is collapsed to "_" before the project name becomes
# a directory name. `project_name` is user-controlled config interpolated into a
# path, so path separators and traversal must not survive.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_project_name(project_name: str) -> str:
    """Reduce a project name to one safe path component.

    Leading/trailing dots are stripped so no name can become ``.``, ``..`` or a
    hidden directory; an empty result falls back to ``unknown`` rather than
    writing records into the traces root itself.
    """
    name = _UNSAFE_NAME_CHARS.sub("_", str(project_name or "").strip()).strip(".")
    return name or "unknown"


def _trace_path(project_name: str, when: Optional[str] = None) -> Path:
    """Monthly trace file for one project, under machine-level carta state.

    Deliberately OUTSIDE the repo (``~/.carta/traces/<project>/``, alongside
    ``~/.carta/registry.json``). Records carry the derived query, which for
    prompts <=500 chars is the prompt verbatim — and `.carta/` is a tracked
    directory in carta projects, so an in-repo location would start committing
    prompt text in every project that upgrades, whatever its .gitignore says.
    """
    from carta.registry import carta_home

    stamp = (when or _utc_now_iso())[:7]          # YYYY-MM
    return carta_home() / "traces" / _safe_project_name(project_name) / f"hook-{stamp}.jsonl"


def build_trace_record(*, query: str, collections: list, hits: list, zone: str,
                       judge: Optional[bool], latency_ms: int, score_kind: str,
                       rrf_k: Optional[int]) -> dict:
    """Build one trace record from a completed search.

    `query` is the DERIVED query (post `_extract_query`), never the raw prompt.
    `zone` is one of "silent" | "judge" | "inject".
    """
    top = hits[0] if hits else None
    return {
        "ts": _utc_now_iso(),
        "query": query,
        "collections": list(collections),
        "n_hits": len(hits),
        "top_source": top.get("source") if top else None,
        "lanes": top.get("lane_ranks") if top else None,
        "score": top.get("score") if top else None,
        "fused_score": top.get("fused_score") if top else None,
        "score_kind": score_kind,
        "rrf_k": rrf_k,
        "zone": zone,
        "judge": judge,
        "latency_ms": latency_ms,
    }


# Single space after the em dash, kept as one constant so the "no matches"
# branch and the per-lane placeholders below can never drift into two
# different renderings of the same message again.
_NOT_IN_LANE = "— not in lane"
_UNKNOWN = "—"


def _fmt_rank(value) -> str:
    """Render a lane rank. `0` is a real rank and must print as "0", not the
    not-in-lane placeholder — hence `is not None`, never a truthiness check."""
    return str(value) if value is not None else _NOT_IN_LANE


def _fmt_value(value) -> str:
    """Render a score/fused_rank/fused_score. A bare `None` must never leak
    into the printed report as the literal text "None"."""
    return str(value) if value is not None else _UNKNOWN


def format_trace(hits: list, needle: str, query: str, collections: list) -> str:
    """Human-readable per-stage report for documents matching `needle`.

    `needle` is matched case-insensitively against each hit's `source` path.
    A hit missing `lane_ranks` (both visual-collection branches and the MCP
    text path produce these) renders as "not in lane", never a crash or a
    fabricated rank 0 (see `.get("lane_ranks")` below, never `["lane_ranks"]`).

    `fused_rank` is reported as-is: it is assigned before the visual cap
    filters, over the whole fetch pool, so it is NOT the hit's final output
    position and must not be presented as one. FINAL is derived from the
    hit's own index in `hits` (the list actually returned to the caller) via
    `enumerate`, never `hits.index(h)` — `list.index` matches the first
    *equal* dict, which is wrong when two hits happen to compare equal.

    `score` (intra-collection dense+sparse RRF, k=2) and `fused_score`
    (cross-collection RRF, k=60) are distinct and labelled as such — never
    conflated. The sparse lane is labelled "bm25 rank": Carta's hybrid lane
    is literally BM25 (see `bm25_model` in config, and "Hybrid (BM25 +
    dense, RRF)" in CLAUDE.md) — "sparse" stays as the internal dict key only.
    """
    lines = [
        f"derived query : {query}",
        f"collections   : {', '.join(collections) or '(none)'}",
        "",
    ]
    needle_lower = needle.lower()
    matches = [(i, h) for i, h in enumerate(hits)
               if needle_lower in str(h.get("source", "")).lower()]
    if not matches:
        lines.append(f"{needle}")
        lines.append(f"  bm25 rank    : {_NOT_IN_LANE}")
        lines.append(f"  dense rank   : {_NOT_IN_LANE}")
        lines.append("  FINAL        : not retrieved")
        lines.append("")
        lines.append("  → never entered retrieval: check ingestion, not ranking.")
        return "\n".join(lines)

    for n, (i, h) in enumerate(matches):
        if n > 0:
            lines.append("")   # separate multiple matched documents
        ranks = h.get("lane_ranks") or {}
        dense = ranks.get("dense")
        sparse = ranks.get("sparse")
        lines.append(str(h.get("source")))
        lines.append(f"  bm25 rank    : {_fmt_rank(sparse)}")
        lines.append(f"  dense rank   : {_fmt_rank(dense)}")
        lines.append(f"  intra-score  : {_fmt_value(h.get('score'))}  (dense+sparse RRF, k=2)")
        lines.append(f"  post-RRF     : fused_rank={_fmt_value(h.get('fused_rank'))}  "
                     f"fused_score={_fmt_value(h.get('fused_score'))}  (cross-collection RRF, k=60)")
        lines.append(f"  FINAL        : {i}  ✓ shown")
    return "\n".join(lines)


def append_trace(project_name: str, record: dict) -> None:
    """Append one JSONL record. Never raises — tracing must not break search.

    Takes the project name (not a repo root): the file lives under
    ``~/.carta/traces/<project>/`` so prompt text can never land in a repo.

    Rotates by `record["ts"]`, not the current time, so a record written by a
    caller that buffers before flushing (or one appended right at a month
    boundary) still lands in the file matching its own timestamp.
    """
    try:
        path = _trace_path(project_name, when=record.get("ts"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
