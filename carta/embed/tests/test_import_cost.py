"""Guard the import cost of the module the proactive-recall hook loads.

`carta-hook` runs on EVERY prompt and blocks submission, so anything imported at
module scope by `carta.embed.pipeline` is paid per prompt before a single line of
hook logic runs. A module-level `from carta.embed.colpali import ...` pulls in
torch + transformers transitively — measured ~2.4 s per prompt — purely to read a
boolean.

Same class of regression as the 0.7.1 fix (ColPali loading a model on every
prompt), one layer lower: that fix stopped the model *loading*; it did not stop
the module being *imported*.

The primary guard is STATIC (AST), deliberately. A runtime `sys.modules` check
passes vacuously wherever torch is not installed — including CI and any dev env
without the `[visual]` extra — so it would have silently guarded nothing exactly
where the regression is cheapest to reintroduce.
"""

import ast
import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest


def _module_level_colpali_imports(module_name: str) -> list[tuple[int, str]]:
    """Return (lineno, imported_name) for every MODULE-LEVEL colpali import.

    Only `tree.body` is inspected, so imports nested inside functions — the
    correct, lazy form — are ignored by construction.
    """
    spec = importlib.util.find_spec(module_name)
    assert spec and spec.origin, f"cannot locate {module_name}"
    tree = ast.parse(pathlib.Path(spec.origin).read_text())

    offenders: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and "colpali" in node.module:
            offenders.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "colpali" in alias.name:
                    offenders.append((node.lineno, alias.name))
    return offenders


def test_pipeline_has_no_module_level_colpali_import():
    """Keep colpali imports function-local in the module the hook loads.

    If this fails, `carta-hook` has silently regained a multi-second fixed cost on
    every prompt in every project.
    """
    offenders = _module_level_colpali_imports("carta.embed.pipeline")
    assert not offenders, (
        "module-level colpali import(s) in carta/embed/pipeline.py: "
        + ", ".join(f"line {ln}: {name}" for ln, name in offenders)
        + " — move the import inside the function that needs it; it drags in "
          "torch/transformers on every carta-hook invocation."
    )


def test_hook_module_has_no_module_level_colpali_import():
    offenders = _module_level_colpali_imports("carta.hook.hook")
    assert not offenders, f"module-level colpali import(s) in the hook: {offenders}"


def test_no_monkeypatch_targets_a_missing_module_attribute():
    """`raising=False` on an attribute that does not exist patches NOTHING, silently.

    This has bitten `test_visual_drainer.py` twice. Both times the sequence was the
    same: a test patched `pipeline.is_colpali_available`, that import later became
    function-local, and `raising=False` turned the patch into a no-op — so the test
    fell through to the real function and asserted nothing. It surfaced only as a
    confusing downstream assertion failure, not as "your patch target is wrong".

    Scans every test module for `monkeypatch.setattr(<module>, "<attr>", ...,
    raising=False)` and fails if `<attr>` is absent from the target. `raising=False`
    is legitimate when creating a genuinely new attribute — but on these
    module-level helpers it has only ever masked drift.
    """
    alias_to_module = {
        "pipeline": "carta.embed.pipeline",
        "embed": "carta.embed.embed",
        "hook": "carta.hook.hook",
        "scoped": "carta.search.scoped",
        "scoped_mod": "carta.search.scoped",
        "rr_mod": "carta.search.rerank",
    }
    call = re.compile(
        r'monkeypatch\.setattr\(\s*([A-Za-z_][\w.]*)\s*,\s*"([^"]+)"[^)]*raising\s*=\s*False'
    )

    loaded: dict[str, object | None] = {}
    offenders: list[str] = []
    root = pathlib.Path(__file__).resolve().parents[2]  # the carta package

    for path in sorted(root.rglob("test_*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            m = call.search(line)
            if not m:
                continue
            target = alias_to_module.get(m.group(1))
            if target is None:
                continue
            if target not in loaded:
                try:
                    loaded[target] = importlib.import_module(target)
                except Exception:
                    loaded[target] = None
            module = loaded[target]
            if module is not None and not hasattr(module, m.group(2)):
                offenders.append(
                    f"{path.relative_to(root)}:{lineno} — "
                    f"{target} has no attribute {m.group(2)!r}"
                )

    assert not offenders, (
        "monkeypatch call(s) that silently patch nothing:\n  "
        + "\n  ".join(offenders)
        + "\n\nPatch the module that DEFINES the name (e.g. "
          '"carta.embed.colpali.is_colpali_available"), and drop raising=False so '
          "a stale target fails loudly."
    )


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None,
    reason="torch not installed — the runtime probe cannot distinguish lazy from absent",
)
def test_pipeline_import_does_not_pull_in_torch_at_runtime():
    """Belt-and-braces runtime confirmation, only where torch actually exists.

    Runs in a subprocess: the heavy modules may already be in sys.modules from
    other tests in this session.
    """
    code = (
        "import sys\n"
        "from carta.embed.pipeline import run_search\n"
        "print('torch' in sys.modules, 'transformers' in sys.modules)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"probe failed:\n{r.stdout}\n{r.stderr}"
    assert r.stdout.strip() == "False False", (
        f"torch/transformers imported transitively: {r.stdout.strip()}"
    )
