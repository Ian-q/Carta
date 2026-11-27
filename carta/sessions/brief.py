"""``carta sessions brief``: the session-start primer.

The most recent sessions of this worktree, then of the rest of the repo, one line
each: date, the first prompt (or the summary when one was written), and the issue
and PR numbers. Built from the payloads already stored in ``{project}_sessions`` —
no embedding, no model call — and never longer than ``max_lines``.

It is also the SessionStart hook's output, so every failure (no config, Qdrant down,
no collection, anything else) yields no lines rather than an error.
"""
from __future__ import annotations

import os
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from carta.sessions.index import sessions_collection
from carta.sessions.project import git_worktrees, locate
from carta.sessions.recall import one_line, refs_text

HARD_MAX_LINES = 15
_LIST_FIELDS = ["session_id", "start_ts", "date", "worktree", "doc_generation"]
_SCROLL_PAGE = 512
_SCROLL_MAX = 20_000      # chunk-0 points read at most (one per session)


def _brief_cfg(cfg: dict) -> dict:
    return ((cfg.get("sessions") or {}).get("brief") or {})


def recent_heads(client: QdrantClient, coll: str) -> list[dict]:
    """Every session's chunk-0 identity fields (not the record), newest first."""
    flt = Filter(must=[FieldCondition(key="chunk_index", match=MatchValue(value=0))])
    heads: dict[str, dict] = {}
    offset = None
    read = 0
    while read < _SCROLL_MAX:
        points, offset = client.scroll(coll, scroll_filter=flt, limit=_SCROLL_PAGE,
                                       offset=offset, with_payload=_LIST_FIELDS,
                                       with_vectors=False)
        read += len(points)
        for p in points:
            pl = p.payload or {}
            sid = pl.get("session_id")
            if not sid:
                continue
            prev = heads.get(sid)
            if prev is None or pl.get("doc_generation", 0) > prev.get("doc_generation", 0):
                heads[sid] = {**pl, "_id": p.id}
        if offset is None:
            break
    return sorted(heads.values(), key=lambda h: (-int(h.get("start_ts") or 0),
                                                 h.get("session_id", "")))


def _line(head: dict, payload: dict, width: int, show_worktree: bool) -> str:
    rec = payload.get("record") or {}
    prompts = rec.get("prompts") or []
    text = rec.get("summary") or (prompts[0] if prompts else "") or payload.get("title", "")
    refs = refs_text(list(payload.get("issues") or []), list(payload.get("prs") or []),
                     max_refs=4)
    prefix = f"  {head.get('date', '')}  "
    if show_worktree and head.get("worktree"):
        prefix += f"[{head['worktree']}] "
    suffix = f"  ({refs})" if refs else ""
    room = max(20, width - len(prefix) - len(suffix))
    return f"{prefix}{one_line(text, room)}{suffix}"


def build_brief(cfg: dict, cwd: str | Path, *, repo_root: Path | None = None,
                client: QdrantClient | None = None, exclude_session: str | None = None,
                per_worktree: int | None = None, per_repo: int | None = None,
                max_lines: int | None = None, width: int | None = None,
                timeout: float = 3) -> list[str]:
    """The primer's lines; ``[]`` when there is nothing to show.

    Errors from Qdrant propagate — ``safe_brief`` is the fail-open wrapper.
    """
    if not (cfg.get("modules") or {}).get("session_memory", True):
        return []
    bc = _brief_cfg(cfg)
    per_worktree = int(bc.get("worktree", 5) if per_worktree is None else per_worktree)
    per_repo = int(bc.get("repo", 5) if per_repo is None else per_repo)
    max_lines = min(HARD_MAX_LINES, int(bc.get("max_lines", HARD_MAX_LINES)
                                        if max_lines is None else max_lines))
    width = int(bc.get("width", 110) if width is None else width)
    if max_lines < 2:
        return []

    coll = sessions_collection(cfg)
    if client is None:
        # No version probe: it is one more round trip at every session start.
        client = QdrantClient(url=cfg["qdrant_url"], timeout=timeout,
                              check_compatibility=False)
    if not client.collection_exists(coll):
        return []
    heads = [h for h in recent_heads(client, coll) if h.get("session_id") != exclude_session]
    if not heads:
        return []

    root = Path(repo_root) if repo_root else None
    loc = locate(str(cwd), root, git_worktrees(root)) if root else None
    here = loc.worktree if loc else None
    repo = loc.repo if loc else (root.name if root else cfg.get("project_name", ""))

    mine = [h for h in heads if here and h.get("worktree") == here][:per_worktree]
    mine_ids = {h["session_id"] for h in mine}
    rest = [h for h in heads if h["session_id"] not in mine_ids][:per_repo]

    sections: list[tuple[str, list[dict], bool]] = []
    if mine:
        label = "main checkout" if loc and loc.worktree_path == loc.repo_root \
            else f"worktree {here}"
        sections.append((f"This {label}:", mine, False))
    if rest:
        sections.append((f"Elsewhere in {repo}:" if mine else f"Recent in {repo}:", rest, True))

    # Fit the cap: one title line, then each section's header and entries. Each step
    # trims the longer section (the repo one on a tie), so both stay visible while
    # there is room and this worktree's section is the last to go.
    budget = max_lines - 1
    sizes = [len(items) for _, items, _ in sections]

    def _total() -> int:
        return sum(n + 1 for n in sizes if n > 0)

    while _total() > budget:
        i = max(range(len(sizes)), key=lambda j: (sizes[j], j))
        sizes[i] -= 1
    if _total() == 0:
        return []
    shown = [h for (_, items, _), n in zip(sections, sizes) for h in items[:n]]
    payloads = {p.id: (p.payload or {}) for p in
                client.retrieve(coll, ids=[h["_id"] for h in shown], with_payload=True,
                                with_vectors=False)}

    lines = ["Past sessions (Carta; `carta recall \"<words>\"` searches them):"]
    for (header, items, show_wt), n in zip(sections, sizes):
        if n <= 0:
            continue
        lines.append(header)
        for h in items[:n]:
            lines.append(_line(h, payloads.get(h["_id"], h), width, show_wt))
    return lines[:max_lines]


def safe_brief(cwd: str | Path | None = None, *, exclude_session: str | None = None,
               **kwargs) -> list[str]:
    """``build_brief`` for a hook: finds the config from ``cwd``, returns ``[]`` on any error."""
    try:
        from carta.config import find_config, load_config
        cwd = Path(cwd or os.getcwd())
        cfg_path = find_config(cwd)
        cfg = load_config(cfg_path)
        return build_brief(cfg, cwd, repo_root=cfg_path.parent.parent,
                           exclude_session=exclude_session, **kwargs)
    except Exception:  # a primer must never break a session start
        return []
