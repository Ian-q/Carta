"""Search past sessions: the read path behind ``carta recall`` and ``carta_recall``.

A query is embedded once and run against ``{project}_sessions`` — hybrid BM25 + dense
when the collection has that schema and ``search.hybrid.enabled`` is set, dense
otherwise — under an optional payload filter (start time, worktree, branch). Chunk
hits are grouped by session: each session keeps its best rank, and up to
``passages`` of its matching chunks, cut down to the lines that share the most
words with the query. The structured record stored on each session's chunk 0 then
supplies the date, title, first prompt, issues and PRs.

No model is called besides the embedding of the query itself.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from carta.sessions.index import session_filter, sessions_collection

DEFAULT_LIMIT = 5
PASSAGE_CHARS = 320

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*")
_STOP = frozenset(
    "the a an and or of to in on for with what was were did we i it is that this how "
    "from at by be do does when which who why where about our my last week".split())


class RecallUnavailable(RuntimeError):
    """Nothing to search: the sessions collection does not exist yet."""


def parse_since(value: str | None, now: float | None = None) -> int | None:
    """Epoch seconds for ``--since``: ``90m``, ``36h``, ``14d``, ``2w`` or ``YYYY-MM-DD``.

    ``None`` or blank means no bound. A date is midnight local time on that day.
    Raises ``ValueError`` on anything else.
    """
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    m = _SINCE_RE.match(value)
    if m:
        now = time.time() if now is None else now
        return int(now - int(m.group(1)) * _UNIT_S[m.group(2).lower()])
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").timestamp())
    except ValueError:
        raise ValueError(
            f"--since {value!r}: expected a span like 14d, 36h, 2w or a date YYYY-MM-DD"
        ) from None


def _terms(text: str) -> set[str]:
    return {w.lower().strip(".-/") for w in _WORD_RE.findall(text or "")
            if len(w) > 2 and w.lower() not in _STOP}


def best_passage(text: str, query: str, max_chars: int = PASSAGE_CHARS) -> str:
    """The lines of a chunk that best match ``query``, as one trimmed passage.

    Scores each non-blank line by how many query words it contains, takes the best
    one (the earliest on a tie) and adds the following lines while they fit.
    Deterministic and model-free: it only chooses what to show, never the ranking.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    q = _terms(query)
    best_i, best_score = 0, -1
    for i, ln in enumerate(lines):
        score = len(q & _terms(ln))
        if ln.startswith("#"):
            score -= 0.5          # a heading is a poor excerpt when a body line also hits
        if score > best_score:
            best_i, best_score = i, score
    out = lines[best_i]
    for ln in lines[best_i + 1:]:
        if len(out) + 1 + len(ln) > max_chars:
            break
        out = f"{out} {ln}"
    out = re.sub(r"\s+", " ", out)
    return out if len(out) <= max_chars else out[:max_chars - 1].rstrip() + "…"


def _query_vector(query: str, cfg: dict) -> list[float]:
    from carta.embed import embed as _embed   # module attribute: patchable in tests
    e = cfg.get("embed") or {}
    return _embed.get_embedding(query, ollama_url=e["ollama_url"], model=e["ollama_model"],
                                prefix="search_query: ")


def _search_points(client: QdrantClient, coll: str, query: str, qvec: list[float],
                   cfg: dict, flt: Filter | None, fetch: int) -> list:
    """Chunk points best first, through the same lanes ``carta search`` uses."""
    from carta.embed.embed import DENSE_VECTOR_NAME, collection_is_hybrid
    hybrid = collection_is_hybrid(client, coll)
    hcfg = (cfg.get("search") or {}).get("hybrid") or {}
    if hybrid and hcfg.get("enabled", False):
        from carta.embed.pipeline import _hybrid_query_collection
        fused = _hybrid_query_collection(
            client, coll, query, qvec, fetch,
            prefetch_limit=max(int(hcfg.get("prefetch_limit", 40)), fetch),
            bm25_model=hcfg.get("bm25_model", "Qdrant/bm25"),
            query_filter=flt, rrf_k=hcfg.get("rrf_k", 2))
        return [(e["point"], e["score"]) for e in fused]
    kwargs = {"using": DENSE_VECTOR_NAME} if hybrid else {}
    resp = client.query_points(collection_name=coll, query=qvec, limit=fetch,
                               query_filter=flt, with_payload=True, **kwargs)
    return [(p, p.score) for p in resp.points]


def fetch_records(client: QdrantClient, coll: str, session_ids: list[str]) -> dict[str, dict]:
    """``{session_id: chunk-0 payload}`` for the given sessions."""
    if not session_ids:
        return {}
    flt = Filter(must=[FieldCondition(key="session_id", match=MatchAny(any=session_ids)),
                       FieldCondition(key="chunk_index", match=MatchValue(value=0))])
    points, _ = client.scroll(coll, scroll_filter=flt, limit=max(len(session_ids) * 2, 10),
                              with_payload=True, with_vectors=False)
    out: dict[str, dict] = {}
    for p in points:
        pl = p.payload or {}
        sid = pl.get("session_id")
        # A re-index briefly leaves two generations; keep the newer one.
        if sid and (sid not in out or pl.get("doc_generation", 0) >
                    out[sid].get("doc_generation", 0)):
            out[sid] = pl
    return out


def session_key(hit: dict) -> str:
    """The string an eval ``expect`` substring is matched against for a session hit."""
    return " | ".join(str(x) for x in (
        f"session:{hit.get('session_id', '')}", hit.get("date", ""), hit.get("worktree", ""),
        hit.get("branch", ""), hit.get("title", "")))


def recall(query: str, cfg: dict, *, since_ts: int | None = None,
           worktree: str | None = None, branch: str | None = None,
           limit: int = DEFAULT_LIMIT, passages: int = 2,
           client: QdrantClient | None = None, timeout: float = 10) -> list[dict]:
    """Past sessions matching ``query``, best first.

    Each result: ``rank``, ``score``, ``session_id``, ``date``, ``started_at``,
    ``ended_at``, ``repo``, ``worktree``, ``branch``, ``title``, ``first_prompt``,
    ``issues``, ``prs``, ``commits``, ``transcript_path``, ``passages`` (list of
    ``{"chunk_index", "text"}``) and ``file_path`` (``session_key``, for the eval).

    Raises ``RecallUnavailable`` when the collection is missing; Qdrant and
    embedding errors propagate.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")
    limit = max(1, int(limit))
    coll = sessions_collection(cfg)
    if client is None:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=timeout)
    if not client.collection_exists(coll):
        raise RecallUnavailable(
            f"no sessions are indexed yet ({coll} does not exist). "
            f"Run: carta sessions index --backfill")

    flt = session_filter(since_ts=since_ts, worktree=worktree or None, branch=branch or None)
    qvec = _query_vector(query, cfg)
    fetch = max(limit * 8, 24)
    order: list[str] = []
    best: dict[str, float] = {}
    chunks: dict[str, list[dict]] = {}
    for point, score in _search_points(client, coll, query, qvec, cfg, flt, fetch):
        pl = point.payload or {}
        sid = pl.get("session_id")
        if not sid:
            continue
        if sid not in best:
            order.append(sid)
            best[sid] = float(score)
            chunks[sid] = []
        if len(chunks[sid]) < passages and (pl.get("text") or "").strip():
            chunks[sid].append({"chunk_index": pl.get("chunk_index"),
                                "text": best_passage(pl["text"], query)})

    order = order[:limit]
    records = fetch_records(client, coll, order)
    results = []
    for rank, sid in enumerate(order, start=1):
        head = records.get(sid, {})
        rec = head.get("record") or {}
        prompts = rec.get("prompts") or []
        hit = {
            "rank": rank,
            "score": round(best[sid], 6),
            "session_id": sid,
            "date": head.get("date", ""),
            "started_at": head.get("started_at", ""),
            "ended_at": head.get("ended_at", ""),
            "repo": head.get("repo", ""),
            "worktree": head.get("worktree", ""),
            "branch": head.get("branch", ""),
            "title": head.get("title", "") or "",
            "first_prompt": prompts[0] if prompts else "",
            "summary": rec.get("summary") or "",
            "issues": list(head.get("issues") or []),
            "prs": list(head.get("prs") or []),
            "commits": list(rec.get("commits") or [])[:10],
            "transcript_path": head.get("transcript_path", ""),
            "transcript_exists": bool(head.get("transcript_path"))
            and Path(head["transcript_path"]).exists(),
            "passages": chunks.get(sid, []),
        }
        hit["file_path"] = session_key(hit)
        results.append(hit)
    return results


def refs_text(issues: list, prs: list, max_refs: int = 6) -> str:
    """``#12 #14 PR #57`` — the refs a line shows, capped."""
    parts = [f"#{n}" for n in issues] + [f"PR #{n}" for n in prs]
    if len(parts) > max_refs:
        parts = parts[:max_refs] + ["…"]
    return " ".join(parts)


def one_line(text: str, width: int) -> str:
    """First non-blank line of ``text``, whitespace collapsed, cut to ``width``."""
    line = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= width else line[:max(1, width - 1)].rstrip() + "…"


def format_results(results: list[dict], width: int = 100) -> str:
    """Human-readable ``carta recall`` output."""
    out = []
    for h in results:
        where = h["worktree"] if h["worktree"] == h["repo"] or not h["repo"] \
            else f"{h['repo']}/{h['worktree']}"
        branch = f" ({h['branch']})" if h["branch"] else ""
        when = h["started_at"][:16].replace("T", " ") if h.get("started_at") else h["date"]
        out.append(f"[{h['rank']}] {when} UTC  {where}{branch}  session {h['session_id']}")
        out.append(f"    {one_line(h['title'] or h['first_prompt'], width)}")
        if h["title"] and h["first_prompt"]:
            out.append(f"    asked: {one_line(h['first_prompt'], width - 7)}")
        refs = refs_text(h["issues"], h["prs"])
        if refs:
            out.append(f"    refs: {refs}")
        for p in h["passages"]:
            out.append(f"    > {p['text']}")
        out.append("")
    return "\n".join(out).rstrip() + ("\n" if out else "")


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------

def sessions_search_fn(cfg: dict, client: QdrantClient | None = None):
    """A ``search_fn(query, k)`` for ``carta.eval.harness.run_eval`` over past sessions.

    Each hit's ``file_path`` is its ``session_key``, which is what the eval's
    ``expect`` / ``reject`` substrings are matched against.
    """
    def _search(query: str, k: int) -> list[dict]:
        nonlocal client
        if client is None:
            client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
        return recall(query, cfg, limit=k, passages=0, client=client)
    return _search


def run_sessions_eval(eval_path: Path, cfg: dict, *, k: int = 5,
                      json_path: str | None = None,
                      client: QdrantClient | None = None) -> dict:
    """Score an eval set against ``{project}_sessions`` and print the result."""
    import json

    from carta.eval.harness import run_eval
    metrics = run_eval(Path(eval_path), sessions_search_fn(cfg, client), k=k)
    print(f"target=sessions  queries={metrics['n_queries']}  "
          f"recall@{k}={metrics['recall_at_k']:.3f}  MRR={metrics['mrr']:.3f}")
    print("  ".join(f"recall@{c}={r:.3f}" for c, r in metrics["recall_at"].items()))
    if metrics["n_with_reject"]:
        print(f"hard negatives: {metrics['reject_violations']}/{metrics['n_with_reject']} "
              f"queries ranked a reject session above the gold")
    for tag, t in metrics["by_tag"].items():
        print(f"  {tag:<16} n={t['n']:<3}  recall@{k}={t['recall_at_k']:.3f}  "
              f"MRR={t['mrr']:.3f}")
    for row in metrics["per_query"]:
        mark = row["first_hit_rank"] if row["first_hit_rank"] is not None else "MISS"
        flag = f"  (reject@{row['first_reject_rank']})" if row["reject_violation"] else ""
        print(f"  [{mark}] {row['q']}{flag}")
    metrics["target"] = "sessions"
    if json_path:
        Path(json_path).write_text(json.dumps(metrics, indent=2))
        print(f"metrics written to {json_path}")
    return metrics
