"""Index session records into ``{project}_sessions`` — the session-memory write path.

Each transcript becomes one record (``carta.sessions.record``), rendered to Markdown
and chunked, header-augmented and embedded by the same code the doc pipeline uses
(``parse.chunk_text``, ``parse.apply_contextual_headers``, ``embed.upsert_chunks``),
so the collection gets the same schema: dense + bm25 hybrid when fastembed is
installed, plain dense otherwise.

Every chunk's payload carries ``session_id``, ``date``, ``start_ts``, ``end_ts``,
``repo``, ``worktree`` and ``branch`` so searches can filter (``session_filter``);
chunk 0 also carries the whole structured record under ``record``, so a session
stays readable after Claude Code has pruned its transcript.

Incremental: a transcript whose size and mtime match the state file is skipped
unopened; one whose SHA-256 still matches is skipped unparsed; one that changed
(a resumed session grows) is rebuilt and re-indexed under a new generation, after
which the session's older points are deleted. State lives outside the repo, at
``<carta home>/sessions/<project>/index-state.json`` unless ``sessions.state_dir``
says otherwise.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Callable

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FilterSelector,
    HasIdCondition,
    MatchValue,
    PayloadSchemaType,
    Range,
)

from carta.config import collection_name
from carta.embed.embed import (
    UPSERT_CLIENT_TIMEOUT_S,
    _point_id_versioned,
    ensure_collection,
    upsert_chunks,
)
from carta.embed.parse import apply_contextual_headers, chunk_text
from carta.sessions.project import candidate_transcripts, git_worktrees, locate
from carta.sessions.record import (
    RECORD_VERSION,
    RecordLimits,
    SessionRecord,
    build_record,
    record_heading,
    render_record,
)
from carta.sessions.transcript import first_cwd, read_transcript

STATE_VERSION = 1
DOC_TYPE = "session"

# Payload fields a session search filters on, with their Qdrant index types.
PAYLOAD_INDEXES = {
    "session_id": PayloadSchemaType.KEYWORD,
    "repo": PayloadSchemaType.KEYWORD,
    "worktree": PayloadSchemaType.KEYWORD,
    "branch": PayloadSchemaType.KEYWORD,
    "date": PayloadSchemaType.KEYWORD,
    "start_ts": PayloadSchemaType.INTEGER,
    "end_ts": PayloadSchemaType.INTEGER,
}


class SessionIndexError(RuntimeError):
    """Writing a record to Qdrant failed; the run stops and the state is left so the
    transcript is retried next time."""


def sessions_collection(cfg: dict) -> str:
    """``{project}_sessions``. Distinct from the older, never-written ``{project}_session``
    that ``carta init`` creates."""
    return collection_name(cfg, "sessions")


def _sessions_cfg(cfg: dict) -> dict:
    return cfg.get("sessions") or {}


def state_path(cfg: dict) -> Path:
    raw = _sessions_cfg(cfg).get("state_dir") or ""
    if raw:
        base = Path(os.path.expanduser(raw))
    else:
        from carta.registry import carta_home
        base = carta_home() / "sessions"
    return base / cfg["project_name"] / "index-state.json"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get("transcripts"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "transcripts": {}}


def save_state(path: Path, state: dict) -> None:
    """Write atomically, so a killed run never leaves a truncated state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------

def ensure_sessions_collection(client: QdrantClient, coll: str) -> None:
    """Create the collection (doc-pipeline schema) and its payload indexes."""
    existed = client.collection_exists(coll)
    ensure_collection(client, coll)
    if existed:
        return
    for field, schema in PAYLOAD_INDEXES.items():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # local mode: "indexes have no effect"
                client.create_payload_index(coll, field_name=field, field_schema=schema)
        except Exception:
            pass  # an index is an optimisation; filtering works without it


def session_filter(*, since_ts: int | None = None, until_ts: int | None = None,
                   repo: str | None = None, worktree: str | None = None,
                   session_id: str | None = None, date: str | None = None,
                   branch: str | None = None) -> Filter | None:
    """A Qdrant filter over session payloads; ``None`` when nothing is constrained.

    ``since_ts`` / ``until_ts`` bound the session's start time (epoch seconds,
    ``until_ts`` exclusive); ``date`` is the local calendar date of the start.
    """
    must = []
    for key, value in (("repo", repo), ("worktree", worktree), ("session_id", session_id),
                       ("date", date), ("branch", branch)):
        if value:
            must.append(FieldCondition(key=key, match=MatchValue(value=value)))
    if since_ts is not None or until_ts is not None:
        must.append(FieldCondition(key="start_ts", range=Range(
            gte=since_ts, lt=until_ts)))
    return Filter(must=must) if must else None


def record_chunks(rec: SessionRecord, cfg: dict, generation: int) -> list[dict]:
    """Chunk dicts for ``upsert_chunks``: the rendered record, chunked like a doc."""
    chunking = (cfg.get("embed") or {}).get("chunking") or {}
    text = render_record(rec)
    chunks = chunk_text([{"page": 1, "text": text, "headings": []}],
                        max_tokens=int(chunking.get("max_tokens", 800)),
                        overlap_fraction=float(chunking.get("overlap_fraction", 0.15)))
    if chunking.get("contextual_header", True):
        apply_contextual_headers(chunks, record_heading(rec), include_section=False)
    base = {
        "slug": rec.session_id,
        "file_path": f"session:{rec.session_id}",
        "doc_type": DOC_TYPE,
        "doc_generation": generation,
        "session_id": rec.session_id,
        "date": rec.date,
        "start_ts": rec.start_ts,
        "end_ts": rec.end_ts,
        "started_at": rec.started_at,
        "ended_at": rec.ended_at,
        "repo": rec.repo,
        "worktree": rec.worktree,
        "branch": rec.branch,
        "title": rec.title,
        "issues": rec.issues,
        "prs": rec.prs,
        "transcript_path": rec.transcript_path,
        "record_version": rec.record_version,
    }
    out = []
    for c in chunks:
        c.pop("page", None)
        c.pop("section_heading", None)
        c.update(base)
        if c["chunk_index"] == 0:
            c["record"] = rec.to_dict()
        out.append(c)
    return out


def index_record(rec: SessionRecord, cfg: dict, client: QdrantClient, generation: int) -> int:
    """Upsert one record's chunks, then delete the session's points from older writes.

    Returns the number of points written. Raises ``SessionIndexError`` when any chunk
    failed to land — older points are kept in that case, so search still has the
    previous version of the record.
    """
    coll = sessions_collection(cfg)
    ensure_sessions_collection(client, coll)
    chunks = [c for c in record_chunks(rec, cfg, generation) if (c.get("text") or "").strip()]
    written = upsert_chunks(chunks, cfg, client=client, collection=coll)
    if written < len(chunks):
        raise SessionIndexError(
            f"only {written} of {len(chunks)} chunk(s) of session {rec.session_id} were "
            f"written to {coll}")
    keep = [_point_id_versioned(c["file_path"], c["chunk_index"], generation) for c in chunks]
    client.delete(coll, points_selector=FilterSelector(filter=Filter(
        must=[FieldCondition(key="session_id", match=MatchValue(value=rec.session_id))],
        must_not=[HasIdCondition(has_id=keep)],
    )))
    return written


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_index(cfg: dict, repo_root: Path, *, transcripts_dir: Path | None = None,
              session_file: Path | None = None, backfill: bool = False,
              force: bool = False, dry_run: bool = False,
              client: QdrantClient | None = None,
              summarize: Callable[[SessionRecord], str | None] | None = None,
              verbose: bool = True) -> dict:
    """Index this project's transcripts. Returns a summary of counts.

    Args:
        cfg: loaded carta config.
        repo_root: the project root (the directory holding ``.carta/``).
        transcripts_dir: overrides ``sessions.transcripts_dir``.
        session_file: index just this transcript (it must belong to the project).
        backfill: consider every transcript, not only those modified within
            ``sessions.window_days``.
        force: rebuild and re-index even when the transcript is unchanged.
        dry_run: build records and report them; write nothing to Qdrant or the state.
        client: Qdrant client; created from ``qdrant_url`` on first need when None.
        summarize: optional callable filling ``SessionRecord.summary`` (e.g. a local
            model). A failure leaves the summary empty and never stops indexing.
        verbose: print one line per indexed session and a closing summary.
    """
    summary = {"indexed": 0, "chunks": 0, "unchanged": 0, "empty": 0, "other_project": 0,
               "unreadable": 0, "considered": 0, "disabled": False, "records": []}
    if not (cfg.get("modules") or {}).get("session_memory", True):
        summary["disabled"] = True
        if verbose:
            print("Session memory is off (modules.session_memory: false).")
        return summary

    scfg = _sessions_cfg(cfg)
    repo_root = Path(repo_root)
    worktrees = git_worktrees(repo_root)
    limits = RecordLimits.from_config(cfg)
    include_sdk = bool(scfg.get("include_sdk_sessions", False))

    if session_file is not None:
        files = [Path(session_file).expanduser()]
    else:
        tdir = Path(os.path.expanduser(str(transcripts_dir or scfg.get("transcripts_dir")
                                           or "~/.claude/projects")))
        files = candidate_transcripts(tdir, [repo_root, *worktrees])
        if not backfill:
            cutoff = time.time() - float(scfg.get("window_days", 3)) * 86400
            files = [f for f in files if _mtime(f) >= cutoff]

    spath = state_path(cfg)
    state = load_state(spath)
    entries: dict = state["transcripts"]

    def _save():
        if not dry_run:
            save_state(spath, state)

    for path in files:
        summary["considered"] += 1
        key = str(path.resolve()) if path.exists() else str(path)
        try:
            st = path.stat()
        except OSError:
            summary["unreadable"] += 1
            continue
        prev = entries.get(key) or {}
        current = prev.get("record_version") == RECORD_VERSION
        if (not force and current and prev.get("size") == st.st_size
                and prev.get("mtime_ns") == st.st_mtime_ns):
            summary["unchanged"] += 1
            continue

        if locate(first_cwd(path) or "", repo_root, worktrees) is None:
            summary["other_project"] += 1
            if session_file is not None and verbose:
                print(f"Skipped {path}: its working directory is outside this project.",
                      file=sys.stderr)
            continue

        sha = _sha256(path)
        if not force and current and prev.get("sha256") == sha:
            prev.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            _save()
            summary["unchanged"] += 1
            continue

        try:
            rec = build_record(read_transcript(path), repo_root=repo_root,
                               worktrees=worktrees, limits=limits, include_sdk=include_sdk)
        except (OSError, ValueError) as e:
            summary["unreadable"] += 1
            print(f"Warning: could not read {path}: {e}", file=sys.stderr)
            continue

        base_entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha,
                      "record_version": RECORD_VERSION}
        if rec is None:
            summary["empty"] += 1
            if not prev.get("chunks"):
                entries[key] = {**base_entry, "session_id": None, "chunks": 0,
                                "generation": prev.get("generation", 0)}
                _save()
            continue

        if summarize is not None:
            try:
                rec.summary = summarize(rec) or None
            except Exception as e:  # an optional nicety must never cost the record
                print(f"Warning: summary for {rec.session_id} failed: {e}", file=sys.stderr)
                rec.summary = None

        summary["records"].append({"session_id": rec.session_id, "date": rec.date,
                                   "worktree": rec.worktree, "prompts": rec.prompts_total,
                                   "title": rec.title})
        if dry_run:
            summary["indexed"] += 1
            if verbose:
                print(f"would index {rec.date} {rec.worktree:<20} {rec.session_id} "
                      f"({rec.prompts_total} prompts)")
            continue

        if client is None:
            client = QdrantClient(url=cfg["qdrant_url"], timeout=UPSERT_CLIENT_TIMEOUT_S)
        generation = int(prev.get("generation", 0)) + 1
        try:
            n = index_record(rec, cfg, client, generation)
        except SessionIndexError:
            raise
        except Exception as e:
            raise SessionIndexError(f"indexing session {rec.session_id} failed: {e}") from e
        entries[key] = {**base_entry, "session_id": rec.session_id, "chunks": n,
                        "generation": generation,
                        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        _save()
        summary["indexed"] += 1
        summary["chunks"] += n
        if verbose:
            print(f"indexed {rec.date} {rec.worktree:<20} {rec.session_id} ({n} chunks)",
                  flush=True)

    if verbose:
        verb = "Would index" if dry_run else "Indexed"
        print(f"{verb} {summary['indexed']} session(s), {summary['chunks']} chunk(s); "
              f"unchanged {summary['unchanged']}, no prompts {summary['empty']}, "
              f"other project {summary['other_project']}.")
    return summary


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
