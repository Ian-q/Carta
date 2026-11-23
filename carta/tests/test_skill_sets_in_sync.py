"""Guard against drift between the two shipped skill sets (#136).

`carta/skills/` is the canonical set: it is the package data `carta init`
installs. The top-level `skills/` is what `.claude-plugin/plugin.json` packages
for the Claude Code plugin, and must mirror it byte-for-byte — with one
deliberate fork: `carta-init`, whose plugin variant says hook registration is
plugin-native while the package variant registers hooks in
`.claude/settings.json`. Only lines about hooks may differ there.

To fix a failure here, edit `carta/skills/<name>/SKILL.md` and copy it over:

    cp carta/skills/<name>/SKILL.md skills/<name>/SKILL.md
"""

import difflib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "carta" / "skills"
PLUGIN = REPO_ROOT / "skills"

# Skills whose plugin copy is an intentional fork, and the word every differing
# line must contain for the fork to stay within its stated purpose.
FORKED = {"carta-init": "hook"}

pytestmark = pytest.mark.skipif(
    not PLUGIN.is_dir(), reason="plugin skills/ not present (installed package, not a checkout)"
)


def _skill_names(root: Path) -> set[str]:
    return {p.parent.name for p in root.glob("*/SKILL.md")}


def test_same_skill_names():
    canonical, plugin = _skill_names(CANONICAL), _skill_names(PLUGIN)
    assert canonical == plugin, (
        f"skill sets differ — missing from plugin skills/: {sorted(canonical - plugin)}; "
        f"plugin-only: {sorted(plugin - canonical)}"
    )


@pytest.mark.parametrize("name", sorted(_skill_names(CANONICAL) - set(FORKED)))
def test_shared_skill_identical(name):
    canonical = (CANONICAL / name / "SKILL.md").read_text()
    plugin_file = PLUGIN / name / "SKILL.md"
    assert plugin_file.exists(), f"skills/{name}/SKILL.md missing"
    assert plugin_file.read_text() == canonical, (
        f"skills/{name}/SKILL.md has drifted from carta/skills/{name}/SKILL.md; "
        f"run: cp carta/skills/{name}/SKILL.md skills/{name}/SKILL.md"
    )


@pytest.mark.parametrize("name,keyword", sorted(FORKED.items()))
def test_forked_skill_differs_only_where_intended(name, keyword):
    canonical = (CANONICAL / name / "SKILL.md").read_text().splitlines()
    plugin = (PLUGIN / name / "SKILL.md").read_text().splitlines()
    changed = [
        line for line in difflib.unified_diff(canonical, plugin, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    stray = [line for line in changed if keyword not in line.lower()]
    assert not stray, (
        f"skills/{name} may differ from carta/skills/{name} only on lines about "
        f"'{keyword}'; unexpected differences: {stray}"
    )
