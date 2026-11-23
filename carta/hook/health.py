"""Health checks for the proactive-recall hook and the stale-scan judge (#138, #83).

Every layer of the recall hook fails open: the wrapper script, the search, the
judge. That is right for a hook that blocks prompt submission, and wrong for the
person trying to find out why nothing ever gets injected. These checks are the
other half of that contract: each silent failure mode the hook can fall into has
a check here that names it, with the command that fixes it.

`carta doctor` turns these into preflight checks (status "fail" is an error and
fails doctor); `carta hook explain` prints them above its per-prompt decision.

Read-only by construction: HTTP GETs against Qdrant/Ollama, and — only with
`probe=True` — one judge call to measure its latency against the configured
timeout. Nothing is pulled, written or embedded.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

WRAPPER_NAME = "carta-prompt-hook.sh"
# The pre-#138 wrapper treated "python3 could not read the config" as "disabled".
_LEGACY_GATE_MARKER = '|| echo "False")'


@dataclass
class HealthItem:
    name: str
    status: str                      # "pass" | "warn" | "fail" | "skip"
    message: str
    suggestion: Optional[str] = None
    details: dict = field(default_factory=dict)
    fix: Optional[Callable[[], bool]] = None


def packaged_wrapper() -> Path:
    return Path(__file__).resolve().parent.parent / "hooks" / WRAPPER_NAME


# ---------------------------------------------------------------------------
# Wrapper script
# ---------------------------------------------------------------------------

def _python3_has_yaml(python3: Optional[str] = None) -> bool:
    exe = python3 or shutil.which("python3")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-c", "import yaml"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def check_wrapper(repo_root: Path, *, python3_has_yaml: Optional[Callable[[], bool]] = None
                  ) -> HealthItem:
    """Is the installed wrapper one that can actually reach carta-hook?"""
    installed = repo_root / ".carta" / "hooks" / WRAPPER_NAME
    src = packaged_wrapper()

    def _refresh() -> bool:
        installed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, installed)
        installed.chmod(installed.stat().st_mode | 0o111)
        return True

    cp_cmd = f"cp {src} .carta/hooks/{WRAPPER_NAME}"
    fix_cmd = f"carta doctor --fix (or: {cp_cmd})"
    if not installed.is_file():
        return HealthItem(
            "recall_hook_wrapper", "warn",
            f".carta/hooks/{WRAPPER_NAME} not installed (fine if the Carta plugin "
            "registers the hook; required if .claude/settings.json points at it)",
            suggestion=cp_cmd, fix=_refresh if src.is_file() else None,
        )
    try:
        text = installed.read_text()
    except OSError as e:
        return HealthItem("recall_hook_wrapper", "fail", f"cannot read wrapper: {e}")

    if _LEGACY_GATE_MARKER in text:
        has_yaml = (python3_has_yaml or _python3_has_yaml)()
        if not has_yaml:
            return HealthItem(
                "recall_hook_wrapper", "fail",
                "hook wrapper never runs carta-hook: it reads the config with the "
                "python3 on PATH, which has no PyYAML, and treats that as "
                "'proactive_recall disabled' — every prompt exits silently",
                suggestion=fix_cmd, fix=_refresh if src.is_file() else None,
                details={"wrapper": str(installed), "python3": shutil.which("python3")},
            )
        return HealthItem(
            "recall_hook_wrapper", "warn",
            "hook wrapper is the old version that silently disables recall whenever "
            "python3 on PATH loses PyYAML (works today)",
            suggestion=cp_cmd, fix=_refresh if src.is_file() else None,
        )
    return HealthItem("recall_hook_wrapper", "pass", "hook wrapper is current")


def check_hook_entry_point() -> HealthItem:
    path = shutil.which("carta-hook")
    if path:
        return HealthItem("recall_hook_entry_point", "pass", f"carta-hook on PATH ({path})")
    return HealthItem(
        "recall_hook_entry_point", "fail",
        "carta-hook is not on PATH — the hook wrapper cannot run it",
        suggestion="Reinstall Carta so its console scripts are on PATH (pipx install carta-cc)",
    )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def check_qdrant(cfg: dict) -> HealthItem:
    url = cfg.get("qdrant_url", "http://localhost:6333")
    budget = cfg.get("proactive_recall", {}).get("search_timeout_s", 3)
    t0 = time.monotonic()
    try:
        r = requests.get(f"{url}/healthz", timeout=max(budget, 3))
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return HealthItem("recall_hook_qdrant", "pass",
                              f"Qdrant reachable at {url} ({ms} ms)", details={"ms": ms})
        msg = f"Qdrant at {url} answered HTTP {r.status_code}"
    except Exception as e:
        msg = f"Qdrant unreachable at {url} ({type(e).__name__}) — every recall search fails open"
    return HealthItem(
        "recall_hook_qdrant", "fail", msg,
        suggestion="Start Qdrant, or correct qdrant_url in .carta/config.yaml",
    )


def ollama_models(url: str, timeout: float = 5) -> list[str]:
    """Names of pulled models, from GET /api/tags. Raises on connection failure."""
    r = requests.get(f"{url}/api/tags", timeout=timeout)
    r.raise_for_status()
    return [m.get("name", "") for m in r.json().get("models", [])]


def model_present(name: str, pulled: list[str]) -> bool:
    if name in pulled:
        return True
    if ":" not in name:
        return f"{name}:latest" in pulled
    return False


def _check_ollama_model(check: str, url: str, model: str, role: str,
                        *, severity: str = "fail") -> HealthItem:
    try:
        pulled = ollama_models(url)
    except Exception as e:
        return HealthItem(
            check, severity,
            f"Ollama unreachable at {url} ({type(e).__name__}) — the {role} cannot run",
            suggestion="Start Ollama (ollama serve), or correct embed.ollama_url",
        )
    if model_present(model, pulled):
        return HealthItem(check, "pass", f"{role} model '{model}' is pulled")
    return HealthItem(
        check, severity,
        f"{role} model '{model}' is not pulled on {url} — every call fails",
        suggestion=f"ollama pull {model}",
        details={"pulled": pulled},
    )


def _timeout_verdict(check: str, role: str, timeout_key: str, timeout_s: float,
                     cold_s: float, warm_s: Optional[float]) -> HealthItem:
    details = {"cold_s": round(cold_s, 2), "timeout_s": timeout_s}
    if warm_s is not None:
        details["warm_s"] = round(warm_s, 2)
    if cold_s <= timeout_s:
        return HealthItem(check, "pass",
                          f"{role} answered in {cold_s:.1f}s (limit {timeout_s}s)",
                          details=details)
    if warm_s is not None and warm_s <= timeout_s:
        return HealthItem(
            check, "warn",
            f"{role} cold start took {cold_s:.1f}s > {timeout_key}={timeout_s}s "
            f"(warm: {warm_s:.1f}s) — the first call after the model unloads times out",
            suggestion=f"Raise {timeout_key}, or keep the model loaded (OLLAMA_KEEP_ALIVE)",
            details=details,
        )
    slow = warm_s if warm_s is not None else cold_s
    return HealthItem(
        check, "fail",
        f"{role} takes {slow:.1f}s but {timeout_key} is {timeout_s}s — every call "
        "times out and is silently treated as 'no answer'",
        suggestion=f"Set {timeout_key} to at least {int(slow * 1.5) + 1}, or use a "
                   "faster/smaller judge model",
        details=details,
    )


def _probe(fn: Callable[[], object], cap_s: float) -> tuple[float, bool]:
    """Time fn(); returns (seconds, answered). fn must itself honour cap_s."""
    t0 = time.monotonic()
    try:
        ok = fn() is not None
    except Exception:
        ok = False
    return time.monotonic() - t0, ok


# ---------------------------------------------------------------------------
# Recall hook
# ---------------------------------------------------------------------------

def check_recall_hook(cfg: dict, repo_root: Path, *, probe: bool = True,
                      python3_has_yaml: Optional[Callable[[], bool]] = None
                      ) -> list[HealthItem]:
    if not cfg.get("modules", {}).get("proactive_recall", False):
        return [HealthItem("recall_hook", "skip",
                           "proactive_recall is disabled in .carta/config.yaml")]

    from carta.hook.hook import _resolve_judge_backend

    items = [
        check_wrapper(repo_root, python3_has_yaml=python3_has_yaml),
        check_hook_entry_point(),
        check_qdrant(cfg),
    ]
    pr = cfg.get("proactive_recall", {})
    ollama_url = cfg.get("embed", {}).get("ollama_url", "http://localhost:11434")
    llm = pr.get("ollama_model", "qwen3.5:0.8b")
    timeout_s = pr.get("judge_timeout_s", 3)
    backend = _resolve_judge_backend(cfg)

    if backend == "ollama":
        present = _check_ollama_model("recall_judge_model", ollama_url, llm,
                                      "recall judge (judge_backend: ollama)")
        items.append(present)
        if probe and present.status == "pass":
            items.append(_probe_ollama_recall_judge(ollama_url, llm, timeout_s))
    else:
        items.extend(_check_crossenc_judge(pr, timeout_s, probe=probe))
        # The LLM is still used to compress prompts over 500 characters into a
        # query. Without it those prompts fall back to their last 500 characters
        # — degraded, not dead — so this is a warning.
        items.append(_check_ollama_model(
            "recall_query_model", ollama_url, llm,
            "long-prompt query extractor (prompts > 500 chars fall back to their tail)",
            severity="warn"))
    return items


def _probe_ollama_recall_judge(url: str, model: str, timeout_s: float) -> HealthItem:
    from carta.hook.judge import ollama_yesno

    cap = max(timeout_s * 5, 30)

    def call():
        return ollama_yesno(url, model, "Answer only 'yes' or 'no'.",
                            "Is this a test?", timeout_s=cap)

    cold, ok = _probe(call, cap)
    if not ok:
        return HealthItem("recall_judge_latency", "fail",
                          f"recall judge gave no answer within {cap:.0f}s",
                          suggestion=f"Check `ollama run {model}` works")
    warm = None
    if cold > timeout_s:
        warm, _ = _probe(call, cap)
    return _timeout_verdict("recall_judge_latency", "recall judge",
                            "proactive_recall.judge_timeout_s", timeout_s, cold, warm)


def _check_crossenc_judge(pr: dict, timeout_s: float, *, probe: bool) -> list[HealthItem]:
    import importlib.util

    model = pr.get("judge_model", "Xenova/ms-marco-MiniLM-L-6-v2")
    try:
        has_fastembed = importlib.util.find_spec("fastembed") is not None
    except Exception:
        has_fastembed = False
    if not has_fastembed:
        return [HealthItem(
            "recall_judge_model", "fail",
            "judge_backend is 'crossenc' but fastembed is not installed — the judge "
            "raises on every grey-zone prompt",
            suggestion="pip install 'carta-cc[hybrid]', or set "
                       "proactive_recall.judge_backend: ollama",
        )]
    from carta.search import rerank
    snapshot = rerank.model_cache_dir() / f"models--{model.replace('/', '--')}"

    def _fetch() -> bool:
        rerank.score_pairs("warmup", ["warmup passage"], model)
        return True

    if not snapshot.exists():
        return [HealthItem(
            "recall_judge_model", "fail",
            f"recall judge model '{model}' is not cached — the hook would download it "
            "while blocking a prompt, overrun the judge timeout and stay silent",
            suggestion="carta doctor --fix", fix=_fetch,
        )]
    items = [HealthItem("recall_judge_model", "pass",
                        f"recall judge (cross-encoder '{model}') cached")]
    if probe:
        # A fresh process per prompt is what the hook is, so the cold time is the
        # one that matters; this process may already have the model loaded, in
        # which case the number is optimistic.
        cold, ok = _probe(lambda: rerank.score_pairs("probe", ["probe passage"], model),
                          timeout_s)
        if not ok:
            items.append(HealthItem("recall_judge_latency", "fail",
                                    "cross-encoder judge raised when scoring a probe pair"))
        else:
            items.append(_timeout_verdict("recall_judge_latency", "recall judge",
                                          "proactive_recall.judge_timeout_s",
                                          timeout_s, cold, None))
    return items


# ---------------------------------------------------------------------------
# Stale-scan judge (#83)
# ---------------------------------------------------------------------------

def git_hook_installed(repo_root: Path) -> bool:
    """Is Carta's managed git hook (the stale-scan shim) installed in any stage?"""
    from carta.hook.git_hook import SENTINEL_START, VALID_STAGES, _hook_path
    for stage in VALID_STAGES:
        try:
            if SENTINEL_START in _hook_path(repo_root, stage).read_text():
                return True
        except (OSError, Exception):
            continue
    return False


def check_stale_scan_judge(cfg: dict, repo_root: Optional[Path] = None, *,
                           probe: bool = True) -> list[HealthItem]:
    """The stale-scan judge only runs from the managed git hook (or `carta claude-md
    check`). A missing model is an error when that hook is installed and a warning
    otherwise, so a project that never installed it is not told it is broken."""
    sc = cfg.get("hooks", {}).get("stale_scan", {})
    if not sc.get("enabled", True):
        return [HealthItem("stale_scan_judge", "skip", "hooks.stale_scan is disabled")]
    url = cfg.get("embed", {}).get("ollama_url", "http://localhost:11434")
    model = sc.get("ollama_model", "qwen3.5:9b")
    timeout_s = sc.get("judge_timeout_s", 60)
    installed = repo_root is not None and git_hook_installed(repo_root)
    present = _check_ollama_model("stale_scan_judge_model", url, model, "stale-scan judge",
                                  severity="fail" if installed else "warn")
    if present.status != "pass":
        present.message += (" (the git-hook scan reports every candidate as a judge error)"
                            if installed else " (git hook not installed)")
    items = [present]
    if probe and present.status == "pass":
        from carta.hook.judge import ollama_json

        def call():
            return ollama_json(url, model, "Respond with a JSON object only.",
                               'Return {"ok": true}.', timeout_s=timeout_s)

        cold, ok = _probe(call, timeout_s)
        if not ok:
            items.append(HealthItem(
                "stale_scan_judge_latency", "fail",
                f"stale-scan judge gave no answer within hooks.stale_scan.judge_timeout_s="
                f"{timeout_s}s — every supersession check would count as a judge error",
                suggestion="Raise hooks.stale_scan.judge_timeout_s or use a faster model",
                details={"elapsed_s": round(cold, 2)},
            ))
        else:
            items.append(_timeout_verdict("stale_scan_judge_latency", "stale-scan judge",
                                          "hooks.stale_scan.judge_timeout_s",
                                          timeout_s, cold, None))
    return items
