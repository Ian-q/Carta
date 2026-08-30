"""Retrieval tracing: what each stage did with each result.

A retrieval miss can happen at six places — never retrieved at all; retrieved in
one lane only; demoted out of the fetch pool by cross-collection fusion; dropped
by the reranker's truncation; dropped by the visual cap; or simply ranked below
`top_n` — and from the outside they are indistinguishable. `format_trace` tells
them apart, but ONLY when handed the per-stage snapshots `run_search` captures
into its `trace_stages` out-param. Without those it can see just the final list,
and says so rather than guessing a cause.

Two corrections to an earlier version of this note, both load-bearing:

* **Dedup is not a loss stage for tracing.** `_dedupe_by_source` keys on `source`,
  which is the same field the needle matches against, so a collapsed duplicate
  always leaves a surviving twin with an identical source. It changes a hit's
  rank; it never removes a traceable document.
* **Plain `top_n` truncation is the dominant loss**, and it was missing from the
  list entirely. With shipped defaults (pool 30, `top_n` 5) most of the pool exits
  there — and it is the case an operator actually runs `--trace` to diagnose.

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

    Three score-shaped fields, deliberately distinct — this instrument exists
    because they were conflated once already:

      `score`        intra-collection dense+sparse RRF (a rank artifact)
      `fused_score`  cross-collection RRF (also a rank artifact)
      `dense_score`  the dense lane's RAW cosine — the ONLY absolute relevance
                     measure here, and the number `hook._gate_zone` compares
                     against `low_threshold` to decide "silent"

    Without `dense_score` a record could not explain its own `zone`, which is
    the whole point of the file (#118). It is as optional as `lane_ranks` —
    both visual branches and the MCP text path omit it, legacy cosine hits
    never have one — so it is read with `.get()` and recorded as null rather
    than dropped: an absent key would be indistinguishable from an older
    schema to anything reading these files later.
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
        "dense_score": top.get("dense_score") if top else None,
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


# The narrowings `run_search` applies, in pipeline order. Only the stages that
# actually ran are recorded (dedup and rerank are both optional), so "where was
# it lost" is answered by the next *recorded* stage after the last one that still
# held the document — never by a stage that never executed.
_STAGE_ORDER = ("retrieved", "fused", "post_dedupe", "rerank_pool", "post_rerank", "final")

# What it means to be absent from a stage, given you were present in the one before.
_STAGE_LOSS = {
    "fused": "demoted out of the fetch pool by cross-collection fusion",
    "post_dedupe": "collapsed into another chunk of the same source by dedup",
    "rerank_pool": "cut by the reranker's candidate_pool slice, before it was scored",
    "post_rerank": "scored by the reranker and ranked out",
    "final": "ranked below top_n",
}

# The visual cap is NOT a stage: `_apply_visual_cap` both enforces the quota and
# hard-truncates to top_n, so its output list cannot tell the two apart — treating
# it as a stage made every plain rank loss report as a visual-cap drop, sending the
# operator to tune `visual_max_ratio` for something it did not cause. run_search
# instead reports exactly the hits the quota rejected, and membership in that set
# overrides the final-stage verdict.
_CAP_DIVERTED = "cap_diverted"
_CAP_LOSS = "dropped by the visual cap"


def _hit_detail_lines(h: dict) -> list:
    """The per-hit rank/score rows, shared by the shown and dropped paths."""
    ranks = h.get("lane_ranks") or {}
    return [
        f"  bm25 rank    : {_fmt_rank(ranks.get('sparse'))}",
        f"  dense rank   : {_fmt_rank(ranks.get('dense'))}",
        f"  dense cosine : {_fmt_value(h.get('dense_score'))}  "
        f"(raw dense-lane similarity, not an RRF score)",
        f"  intra-score  : {_fmt_value(h.get('score'))}  (dense+sparse RRF, k=2)",
        f"  post-RRF     : fused_rank={_fmt_value(h.get('fused_rank'))}  "
        f"fused_score={_fmt_value(h.get('fused_score'))}  (cross-collection RRF, k=60)",
    ]


def _find_in_stage(stage_hits, needle_lower: str):
    """First hit in `stage_hits` whose source contains the needle, else None."""
    for h in stage_hits or []:
        if needle_lower in str(h.get("source", "")).lower():
            return h
    return None


def _trace_survival(stages: dict, needle_lower: str):
    """Walk the recorded stages in pipeline order.

    Returns `(last_stage_seen, hit, lost_at)` — where `lost_at` is the first
    recorded stage that no longer holds the document, or None if it survived to
    the last recorded stage. `last_stage_seen` is None when the document never
    appears in any stage at all, which is the only case that genuinely indicts
    ingestion.
    """
    recorded = [s for s in _STAGE_ORDER if s in stages]
    last_seen, hit = None, None
    for stage in recorded:
        found = _find_in_stage(stages.get(stage), needle_lower)
        if found is not None:
            last_seen, hit = stage, found
        elif last_seen is not None:
            return last_seen, hit, stage
    return last_seen, hit, None


def format_trace(hits: list, needle: str, query: str, collections: list,
                 *, stages: dict | None = None) -> str:
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

    `score` (intra-collection dense+sparse RRF, k=2), `fused_score`
    (cross-collection RRF, k=60) and `dense_score` (the dense lane's raw
    cosine) are three different numbers and are labelled as three different
    numbers — never conflated. `dense_score` is the one the recall hook gates
    on, so it is shown even though it takes no part in ordering; like the other
    optional fields it renders as the "—" placeholder when absent (a
    sparse-only hit, or any visual/MCP hit, has no cosine to report).

    The sparse lane is labelled "bm25 rank": Carta's hybrid lane is literally
    BM25 (see `bm25_model` in config, and "Hybrid (BM25 + dense, RRF)" in
    CLAUDE.md) — "sparse" stays as the internal dict key only.
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
        last_seen, hit, lost_at = (
            _trace_survival(stages, needle_lower) if stages else (None, None, None)
        )
        if hit is not None:
            # It WAS retrieved. Show the evidence and name the narrowing that
            # dropped it — this is a ranking-stage loss, not an ingestion one.
            lines.append(str(hit.get("source") or needle))
            lines.extend(_hit_detail_lines(hit))
            # A quota casualty is reported as such regardless of which stage the
            # walk landed on — the cap runs inside the same step that truncates.
            if _find_in_stage(stages.get(_CAP_DIVERTED), needle_lower) is not None:
                reason = _CAP_LOSS
            else:
                reason = _STAGE_LOSS.get(lost_at, "dropped after retrieval")
            lines.append(f"  FINAL        : not shown — {reason}")
            lines.append("")
            lines.append("  → retrieved, embedded and ranked — then dropped by the "
                         "narrowing named above. This is a ranking problem, not an "
                         "ingestion one.")
            return "\n".join(lines)

        lines.append(f"{needle}")
        lines.append(f"  bm25 rank    : {_NOT_IN_LANE}")
        lines.append(f"  dense rank   : {_NOT_IN_LANE}")
        lines.append("  FINAL        : not retrieved")
        lines.append("")
        if stages:
            # Stage data was captured and the document is in none of it.
            lines.append("  → never entered retrieval: check ingestion, not ranking.")
        else:
            # No stage data: "absent from the final results" is all that is known,
            # and the two causes are indistinguishable from here. Say so rather
            # than assert a cause this call cannot establish.
            lines.append("  → absent from the final results. Without stage data this "
                         "cannot tell a never-retrieved document from one that was "
                         "retrieved and then out-ranked.")
        return "\n".join(lines)

    for n, (i, h) in enumerate(matches):
        if n > 0:
            lines.append("")   # separate multiple matched documents
        lines.append(str(h.get("source")))
        lines.extend(_hit_detail_lines(h))
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
