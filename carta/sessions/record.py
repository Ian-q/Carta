"""Build one deterministic session record from a transcript. No model call.

The record keeps what a later search needs to find the session and what a reader
needs once the transcript itself has been pruned: where and when it ran, what the
user typed (verbatim — the highest-signal text in a transcript), which files were
edited, which issues, PRs and commits it touched, and the final assistant message.

Hook output, injected context, task notifications, tool results and subagent
sidechains are all excluded from the prompts.

``SessionRecord.summary`` is the seam for an optional local-model summary: the
builder always leaves it ``None``, ``render_record`` includes it when set, and the
indexer accepts a ``summarize`` callable that fills it before rendering.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from carta.sessions.project import Location, locate
from carta.sessions.transcript import TranscriptFile, block_text, content_blocks

# Bump when the record's content or rendering changes, so `carta sessions index`
# rebuilds records written by an older builder.
RECORD_VERSION = 1

EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Text a user line carries that the user did not type.
_NOISE_PREFIXES = (
    "<task-notification>", "<local-command-", "<bash-input>", "<bash-stdout>",
    "<bash-stderr>", "<system-reminder>", "<user-prompt-submit-hook>",
    "[Request interrupted", "Caveat: The messages below",
)
_COMMIT_CMD_RE = re.compile(r"\bgit\b[^|;&\n]*?\bcommit\b")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b", re.S)
_MSG_RE = re.compile(r"""(?:\s-[a-zA-Z]*m|--message)(?:\s+|=)(["'])(.*?)(?<!\\)\1""", re.S)
_COMMIT_RE = re.compile(r"^\[([^\s\]]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.+)$", re.M)
_PR_RES = (
    re.compile(r"\bpull/(\d{1,6})\b"),
    re.compile(r"\bPR\s*#?(\d{1,6})\b"),
    re.compile(r"\bpull request\s*#?(\d{1,6})\b", re.I),
    re.compile(r"\bgh pr (?:view|checkout|merge|diff|checks|comment|edit|close|review|ready)"
               r" (\d{1,6})\b"),
)
_ISSUE_RES = (
    re.compile(r"\bissues/(\d{1,6})\b"),
    re.compile(r"\bgh issue (?:view|comment|close|edit|reopen) (\d{1,6})\b"),
    re.compile(r"(?<![\w&/#])#(\d{1,6})\b"),
)


@dataclass
class RecordLimits:
    """Size caps, so one runaway session cannot dominate the index."""

    max_prompts: int = 60
    max_prompt_chars: int = 2000
    max_final_chars: int = 3000
    max_files: int = 100
    max_refs: int = 50
    max_commits: int = 50

    @classmethod
    def from_config(cls, cfg: dict) -> "RecordLimits":
        raw = ((cfg.get("sessions") or {}).get("limits") or {})
        known = {k: int(v) for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class SessionRecord:
    session_id: str
    transcript_path: str
    repo: str
    repo_root: str
    worktree: str
    worktree_path: str
    cwd: str
    branch: str
    entrypoint: str          # how the session was driven: "cli", "sdk-py", ... ("" if unknown)
    started_at: str          # ISO-8601 UTC
    ended_at: str
    start_ts: int            # epoch seconds, for range filters
    end_ts: int
    date: str                # local calendar date of the start, YYYY-MM-DD
    title: str = ""
    prompts: list[str] = field(default_factory=list)
    prompts_total: int = 0
    files_edited: list[str] = field(default_factory=list)
    issues: list[int] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    final_text: str = ""
    summary: str | None = None
    record_version: int = RECORD_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------

def typed_prompt(entry: dict, *, allow_sdk: bool = False) -> str | None:
    """The text the user typed on this line, or ``None`` if it is anything else.

    Lines a program wrote through the Agent SDK (``promptSource: "sdk"``) count only
    with ``allow_sdk``: by default a scripted session is not a person's session.
    """
    if entry.get("type") != "user":
        return None
    if (entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary")
            or entry.get("isVisibleInTranscriptOnly")):
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    source = entry.get("promptSource")
    if source == "system" or (source == "sdk" and not allow_sdk):
        return None
    blocks = content_blocks(entry)
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    text = "\n".join(block_text(b) for b in blocks if b.get("type") == "text")
    if "<command-name>" in text:
        name = _CMD_NAME_RE.search(text)
        args = _CMD_ARGS_RE.search(text)
        text = f"{name.group(1).strip() if name else ''} {args.group(1).strip() if args else ''}"
    text = _REMINDER_RE.sub("", text).strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None
    return text


def commit_subject(command: str) -> str | None:
    """Subject line of the commit a ``git commit`` shell command makes, when the
    message is on the command line (``-m "..."`` or a here-document). Needed because
    agents commit with ``-q``, whose output does not repeat the subject.
    """
    if not _COMMIT_CMD_RE.search(command):
        return None
    m = _HEREDOC_RE.search(command)
    body = m.group(2) if m else None
    if body is None:
        m = _MSG_RE.search(command)
        body = m.group(2) if m else None
    if not body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return None


def _parse_ts(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cap_text(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _cap_list_ends(items: list, n: int) -> list:
    """Keep the first and last halves of ``items`` when it is longer than ``n``."""
    if len(items) <= n:
        return list(items)
    head = (n + 1) // 2
    tail = n - head
    return list(items[:head]) + (list(items[-tail:]) if tail else [])


def _rel(path: str, loc: Location) -> str:
    """``path`` relative to the session's worktree (or the repo root) when inside it."""
    for candidate in (path, os.path.realpath(path)):
        for base in (loc.worktree_path, loc.repo_root):
            if candidate == base or candidate.startswith(base.rstrip("/") + "/"):
                return candidate[len(base):].lstrip("/") or "."
    return path


def _refs(texts: list[str]) -> tuple[list[int], list[int]]:
    prs: set[int] = set()
    issues: set[int] = set()
    for t in texts:
        for rx in _PR_RES:
            prs.update(int(m) for m in rx.findall(t))
    for t in texts:
        for rx in _ISSUE_RES:
            issues.update(int(m) for m in rx.findall(t))
    prs.discard(0)
    issues.discard(0)
    return sorted(prs), sorted(issues - prs)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_record(tf: TranscriptFile, *, repo_root: Path, worktrees: list[Path] | None = None,
                 limits: RecordLimits | None = None,
                 include_sdk: bool = False) -> SessionRecord | None:
    """Build the session record for one transcript.

    Returns ``None`` when the transcript has no typed prompt (nothing a person asked
    for) or when its working directory is outside the project. ``include_sdk`` also
    counts prompts a program sent through the Agent SDK (see ``typed_prompt``).
    """
    limits = limits or RecordLimits()
    main = [e for e in tf.entries if not e.get("isSidechain")]

    cwd = next((e["cwd"] for e in main if isinstance(e.get("cwd"), str) and e["cwd"]), "")
    loc = locate(cwd, repo_root, worktrees)
    if loc is None:
        return None

    prompts: list[str] = []
    final_text = ""
    files: list[str] = []
    commits: list[str] = []
    ref_texts: list[str] = []
    pr_links: set[int] = set()
    branch = ""
    ai_title = custom_title = ""
    times: list[datetime] = []
    pending_commits: dict[str, str] = {}   # tool_use id -> subject from the command
    entrypoints: dict[str, int] = {}

    for e in main:
        t = e.get("type")
        dt = _parse_ts(e.get("timestamp"))
        if dt is not None and t in ("user", "assistant", "system", "attachment"):
            times.append(dt)
        if isinstance(e.get("gitBranch"), str) and e["gitBranch"]:
            branch = e["gitBranch"]
        if t == "ai-title" and isinstance(e.get("aiTitle"), str):
            ai_title = e["aiTitle"].strip()
        elif t == "custom-title" and isinstance(e.get("customTitle"), str):
            custom_title = e["customTitle"].strip()
        elif t == "pr-link" and isinstance(e.get("prNumber"), int):
            pr_links.add(e["prNumber"])
        elif t == "user":
            if isinstance(e.get("entrypoint"), str):
                entrypoints[e["entrypoint"]] = entrypoints.get(e["entrypoint"], 0) + 1
            p = typed_prompt(e, allow_sdk=include_sdk)
            if p is not None:
                if not prompts or prompts[-1] != p:
                    prompts.append(p)
                ref_texts.append(p)
                continue
            for b in content_blocks(e):
                if b.get("type") != "tool_result":
                    continue
                subjects = [m.group(3).strip() for m in _COMMIT_RE.finditer(block_text(b))]
                pending = pending_commits.pop(b.get("tool_use_id"), None)
                if pending and not b.get("is_error") and not subjects:
                    subjects = [pending]
                for subject in subjects:
                    if subject not in commits:
                        commits.append(subject)
        elif t == "assistant" and not e.get("isApiErrorMessage"):
            texts = []
            for b in content_blocks(e):
                bt = b.get("type")
                if bt == "text" and block_text(b).strip():
                    texts.append(block_text(b))
                elif bt == "tool_use":
                    name = b.get("name")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    key = EDIT_TOOLS.get(name)
                    if key and isinstance(inp.get(key), str):
                        rel = _rel(inp[key], loc)
                        if rel not in files:
                            files.append(rel)
                    elif name == "Bash" and isinstance(inp.get("command"), str):
                        ref_texts.append(inp["command"])
                        subject = commit_subject(inp["command"])
                        if subject and isinstance(b.get("id"), str):
                            pending_commits[b["id"]] = subject
            if texts:
                final_text = "\n\n".join(texts)
                ref_texts.extend(texts)

    if not prompts:
        return None

    ref_texts.extend(commits)
    prs, issues = _refs(ref_texts)
    prs = sorted(set(prs) | pr_links)
    issues = [i for i in issues if i not in prs]

    if times:
        start, end = min(times), max(times)
    else:
        start = end = datetime.fromtimestamp(tf.path.stat().st_mtime, tz=timezone.utc)

    return SessionRecord(
        session_id=tf.session_id,
        transcript_path=str(tf.path),
        repo=loc.repo,
        repo_root=loc.repo_root,
        worktree=loc.worktree,
        worktree_path=loc.worktree_path,
        cwd=cwd,
        branch=branch,
        entrypoint=max(entrypoints, key=entrypoints.get) if entrypoints else "",
        started_at=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        ended_at=end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        date=local_date(int(start.timestamp())),
        title=custom_title or ai_title,
        prompts=[_cap_text(p, limits.max_prompt_chars)
                 for p in _cap_list_ends(prompts, limits.max_prompts)],
        prompts_total=len(prompts),
        files_edited=files[:limits.max_files],
        issues=issues[:limits.max_refs],
        prs=prs[:limits.max_refs],
        commits=commits[:limits.max_commits],
        final_text=_cap_text(final_text, limits.max_final_chars),
    )


def local_date(epoch_s: int) -> str:
    """Calendar date of an epoch time in this machine's local timezone."""
    return datetime.fromtimestamp(epoch_s).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def record_heading(rec: SessionRecord) -> str:
    """One-line identity of a session: the contextual header for its chunks."""
    where = rec.repo if rec.worktree == rec.repo else f"{rec.repo}/{rec.worktree}"
    branch = f" ({rec.branch})" if rec.branch else ""
    return f"Session {rec.date} {where}{branch}"


def render_record(rec: SessionRecord) -> str:
    """The record as Markdown: the text that is chunked and embedded."""
    def _local(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    title = rec.title or rec.prompts[0].splitlines()[0][:100]
    lines = [f"# {title}", ""]
    meta = [f"{_local(rec.start_ts)} to {_local(rec.end_ts)}", f"repo {rec.repo}",
            f"worktree {rec.worktree}"]
    if rec.branch:
        meta.append(f"branch {rec.branch}")
    lines.append(" · ".join(meta))
    refs = []
    if rec.issues:
        refs.append("Issues: " + ", ".join(f"#{n}" for n in rec.issues))
    if rec.prs:
        refs.append("PRs: " + ", ".join(f"#{n}" for n in rec.prs))
    if refs:
        lines.append(" · ".join(refs))
    if rec.summary:
        lines += ["", "## Summary", "", rec.summary.strip()]
    lines += ["", "## Prompts", ""]
    lines += [f"{i}. {p}" for i, p in enumerate(rec.prompts, 1)]
    if rec.prompts_total > len(rec.prompts):
        lines.append(f"({rec.prompts_total - len(rec.prompts)} more prompts in the middle "
                     f"of the session are not shown)")
    if rec.files_edited:
        lines += ["", "## Files edited", ""] + [f"- {f}" for f in rec.files_edited]
    if rec.commits:
        lines += ["", "## Commits", ""] + [f"- {c}" for c in rec.commits]
    if rec.final_text:
        lines += ["", "## Final message", "", rec.final_text]
    return "\n".join(lines) + "\n"
