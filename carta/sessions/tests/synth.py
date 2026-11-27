"""Builders for synthetic Claude Code transcripts.

Every line here is invented. The shapes follow the transcript JSONL format
(``type``, ``timestamp``, ``cwd``, ``gitBranch``, ``message.content`` blocks,
``isSidechain``, ``isMeta``), but no text comes from a real session.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def ts(minutes: float) -> str:
    """ISO timestamp ``minutes`` after T0, in the transcript's ``...Z`` form."""
    t = T0 + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Transcript:
    """Accumulates transcript lines for one session and writes them as JSONL."""

    def __init__(self, session_id: str, cwd: str, branch: str = "main",
                 start_minutes: float = 0.0):
        self.session_id = session_id
        self.cwd = cwd
        self.branch = branch
        self.t = start_minutes
        self.lines: list[dict] = []
        self._n = 0

    # -- low level ---------------------------------------------------------
    def _base(self, type_: str, **extra) -> dict:
        self._n += 1
        self.t += 1
        d = {
            "type": type_,
            "uuid": f"{self.session_id}-{self._n}",
            "sessionId": self.session_id,
            "timestamp": ts(self.t),
            "cwd": self.cwd,
            "gitBranch": self.branch,
            "isSidechain": False,
            "userType": "external",
        }
        d.update(extra)
        return d

    def raw(self, line: dict) -> "Transcript":
        self.lines.append(line)
        return self

    # -- user side ---------------------------------------------------------
    def prompt(self, text: str, *, with_origin: bool = True) -> "Transcript":
        extra = {"promptSource": "typed", "origin": {"kind": "human"}} if with_origin else {}
        self.lines.append(self._base("user", message={"role": "user", "content": text}, **extra))
        return self

    def prompt_blocks(self, text: str) -> "Transcript":
        content = [{"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                   {"type": "text", "text": text}]
        self.lines.append(self._base(
            "user", message={"role": "user", "content": content},
            promptSource="typed", origin={"kind": "human"}))
        return self

    def meta(self, text: str) -> "Transcript":
        self.lines.append(self._base(
            "user", isMeta=True,
            message={"role": "user", "content": [{"type": "text", "text": text}]}))
        return self

    def notification(self, text: str) -> "Transcript":
        self.lines.append(self._base(
            "user", message={"role": "user",
                             "content": f"<task-notification>{text}</task-notification>"},
            promptSource="system", origin={"kind": "task-notification"}))
        return self

    def slash(self, name: str, args: str = "") -> "Transcript":
        body = (f"<command-message>{name.lstrip('/')}</command-message>\n"
                f"<command-name>{name}</command-name>\n"
                f"<command-args>{args}</command-args>")
        self.lines.append(self._base("user", message={"role": "user", "content": body}))
        return self

    def compact_summary(self, text: str) -> "Transcript":
        self.lines.append(self._base(
            "user", isCompactSummary=True, message={"role": "user", "content": text}))
        return self

    def tool_result(self, tool_id: str, text: str) -> "Transcript":
        self.lines.append(self._base("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
            toolUseResult={"stdout": text}))
        return self

    # -- assistant side ----------------------------------------------------
    def say(self, text: str, *, sidechain: bool = False) -> "Transcript":
        line = self._base("assistant", message={
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": text}]})
        line["isSidechain"] = sidechain
        self.lines.append(line)
        return self

    def tool(self, name: str, tool_input: dict, tool_id: str = "t1",
             *, sidechain: bool = False) -> "Transcript":
        line = self._base("assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]})
        line["isSidechain"] = sidechain
        self.lines.append(line)
        return self

    def sidechain_prompt(self, text: str) -> "Transcript":
        line = self._base("user", message={"role": "user", "content": text})
        line["isSidechain"] = True
        self.lines.append(line)
        return self

    # -- non-message lines -------------------------------------------------
    def title(self, text: str, kind: str = "ai-title") -> "Transcript":
        key = "aiTitle" if kind == "ai-title" else "customTitle"
        self.lines.append({"type": kind, "sessionId": self.session_id, key: text})
        return self

    def pr_link(self, number: int) -> "Transcript":
        self.lines.append({"type": "pr-link", "sessionId": self.session_id,
                           "prNumber": number, "prRepository": "example/widgets",
                           "prUrl": f"https://example.invalid/pull/{number}",
                           "timestamp": ts(self.t)})
        return self

    def system(self, subtype: str = "turn_duration") -> "Transcript":
        self.lines.append(self._base("system", subtype=subtype, content=""))
        return self

    # -- output ------------------------------------------------------------
    def write(self, path: Path, *, garbage_line: bool = False) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for line in self.lines:
                f.write(json.dumps(line) + "\n")
            if garbage_line:
                f.write('{"type": "user", "message": {"content": "trunc')  # partial write
        return path

    def append_to(self, path: Path, lines: list[dict]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")


def slug_for(path: str) -> str:
    """Claude Code's transcript directory name for a cwd."""
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def standard_session(cwd: str, session_id: str = "sess-alpha", branch: str = "feat/widgets",
                     start_minutes: float = 0.0) -> Transcript:
    """A session with every kind of noise around three typed prompts."""
    tr = Transcript(session_id, cwd, branch=branch, start_minutes=start_minutes)
    tr.raw({"type": "permission-mode", "sessionId": session_id, "permissionMode": "default"})
    tr.meta("<system-reminder>hook context that is not the user</system-reminder>")
    tr.prompt("Please make the widget counter thread-safe, see issue #42")
    tr.say("Looking at the counter now.")
    tr.tool("Read", {"file_path": f"{cwd}/src/counter.py"}, "t1")
    tr.tool_result("t1", "def bump(): ...")
    tr.tool("Edit", {"file_path": f"{cwd}/src/counter.py", "old_string": "a",
                     "new_string": "b"}, "t2")
    tr.tool_result("t2", "The file was updated")
    tr.notification("background job finished")
    tr.prompt("<system-reminder>injected</system-reminder>also add a test for it")
    tr.tool("Write", {"file_path": f"{cwd}/tests/test_counter.py", "content": "x"}, "t3")
    tr.tool("Bash", {"command": "git commit -m 'Make counter thread-safe'"}, "t4")
    tr.tool_result("t4", "[feat/widgets 1a2b3c4] Make counter thread-safe\n 2 files changed")
    tr.slash("/review", "carefully")
    tr.compact_summary("This session is being continued from a previous conversation.")
    tr.prompt("[Request interrupted by user]")
    tr.sidechain_prompt("Subagent task: survey the codebase")
    tr.say("Subagent answer that should not be the final text", sidechain=True)
    tr.tool("Edit", {"file_path": f"{cwd}/src/sidechain_only.py"}, "t9", sidechain=True)
    tr.prompt("open a PR when done")
    tr.tool("Bash", {"command": "gh pr create --title 'Counter' --body 'Fixes #42'"}, "t5")
    tr.pr_link(57)
    tr.system("stop_hook_summary")
    tr.title("Thread-safe widget counter")
    tr.say("Done: the counter is locked and PR #57 is open.")
    return tr
