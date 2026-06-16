"""Install/remove a managed git-hook shim that runs `carta hook check`.

The shim is wrapped in sentinel comments so it can live alongside (chained into)
a user's existing hook and be removed cleanly."""
from __future__ import annotations

from pathlib import Path

SENTINEL_START = "# >>> carta managed >>>"
SENTINEL_END = "# <<< carta managed <<<"
VALID_STAGES = ("pre-push", "pre-commit")


def _shim_block(stage: str) -> str:
    # PATH-guarded so the hook fails open when `carta` is not on PATH (e.g. GUI
    # git clients that don't inherit the user's shell PATH) — a missing binary
    # must never block a push/commit. Only a real stale-scan failure exits non-zero.
    return (
        f"{SENTINEL_START}\n"
        f"if command -v carta >/dev/null 2>&1; then\n"
        f"  carta hook check --stage {stage} || exit $?\n"
        f"fi\n"
        f"{SENTINEL_END}\n"
    )


def _hook_path(repo_root: Path, stage: str) -> Path:
    return repo_root / ".git" / "hooks" / stage


def install_hook(repo_root: Path, stage: str) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown hook stage: {stage}")
    hook = _hook_path(repo_root, stage)
    hook.parent.mkdir(parents=True, exist_ok=True)
    if not hook.exists():
        hook.write_text("#!/bin/sh\n" + _shim_block(stage), encoding="utf-8")
        hook.chmod(0o755)
        return "installed"
    existing = hook.read_text(encoding="utf-8")
    if SENTINEL_START in existing:
        return "already-installed"
    raise FileExistsError(
        f"A non-Carta {stage} hook already exists at {hook}. "
        f"Chain Carta in by adding this line:\n"
        f"  carta hook check --stage {stage} || exit $?"
    )


def uninstall_hook(repo_root: Path, stage: str) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown hook stage: {stage}")
    hook = _hook_path(repo_root, stage)
    if not hook.exists():
        return "absent"
    text = hook.read_text(encoding="utf-8")
    if SENTINEL_START not in text:
        return "not-managed"
    out, skipping = [], False
    for ln in text.splitlines(keepends=True):
        stripped = ln.strip()
        if stripped == SENTINEL_START:
            skipping = True
            continue
        if stripped == SENTINEL_END:
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    remaining = "".join(out).strip()
    if remaining in ("", "#!/bin/sh"):
        hook.unlink()
        return "removed-file"
    hook.write_text("".join(out), encoding="utf-8")
    return "removed-block"
