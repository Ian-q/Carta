"""Read Claude Code JSONL transcripts (``~/.claude/projects/<slug>/<session>.jsonl``).

A transcript is one JSON object per line. The lines this package uses:

- ``type: "user"`` / ``"assistant"`` — conversation turns, with ``message.content``
  either a string or a list of blocks (``text``, ``tool_use``, ``tool_result``,
  ``thinking``, ``image``). They carry ``timestamp``, ``cwd``, ``gitBranch``,
  ``isSidechain`` (subagent traffic), ``isMeta`` (injected context) and, on newer
  versions, ``origin.kind`` / ``promptSource`` saying who wrote a user line.
- ``type: "ai-title"`` / ``"custom-title"`` — the session's title.
- ``type: "pr-link"`` — a pull request the session opened or touched.

Everything else (snapshots, request bookkeeping, permission modes, ...) is ignored.
Reading is tolerant: a line that is not valid JSON — typically the partial last line
of a session still being written — is skipped, never fatal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TranscriptFile:
    """The parsed lines of one transcript, plus where they came from."""

    path: Path
    entries: list[dict] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def session_id(self) -> str:
        for e in self.entries:
            sid = e.get("sessionId")
            if isinstance(sid, str) and sid:
                return sid
        return self.path.stem


def iter_lines(path: Path):
    """Yield each parsed JSON object in ``path``; yields ``None`` for a bad line."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                yield None
                continue
            yield obj if isinstance(obj, dict) else None


def read_transcript(path: Path) -> TranscriptFile:
    """Parse a whole transcript file."""
    tf = TranscriptFile(path=Path(path))
    for obj in iter_lines(tf.path):
        if obj is None:
            tf.bad_lines += 1
        else:
            tf.entries.append(obj)
    return tf


def first_cwd(path: Path, max_lines: int = 200) -> str | None:
    """The first non-sidechain ``cwd`` recorded in a transcript, reading as little as
    possible. Used to decide whether a transcript belongs to a project before parsing
    all of it. ``None`` when none is found in the first ``max_lines`` lines.
    """
    try:
        for i, obj in enumerate(iter_lines(Path(path))):
            if i >= max_lines:
                break
            if obj and not obj.get("isSidechain") and isinstance(obj.get("cwd"), str):
                return obj["cwd"]
    except OSError:
        return None
    return None


def content_blocks(entry: dict) -> list[dict]:
    """``message.content`` as a list of blocks (a bare string becomes one text block)."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def block_text(block: dict) -> str:
    """Plain text of a block; for tool_result, the concatenated text of its content."""
    if block.get("type") == "text":
        t = block.get("text")
        return t if isinstance(t, str) else ""
    if block.get("type") == "tool_result":
        c = block.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(b.get("text", "") for b in c
                             if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""
