# Carta Doctor Qdrant Start + Search Error Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `carta doctor` interactively offer to start Qdrant, add macOS-specific Docker tips, and make `carta search` fail clearly when Qdrant is down.

**Architecture:** Three targeted changes across four files. `preflight.py` gains OS-aware Docker tip. `cli.py` triggers interactive auto-fix after every `carta doctor` run (not just `--fix`). `pipeline.py` catches Qdrant connection errors from `query_points()` and raises them instead of silently returning empty results.

**Tech Stack:** Python 3.10+, requests, qdrant-client, subprocess (Docker), unittest.mock

---

## Files Modified

- `carta/install/preflight.py` — `_check_docker_running()`: OS-aware suggestion on macOS
- `carta/cli.py` — `cmd_doctor()`: trigger interactive fix prompt for fixable failures without requiring `--fix`
- `carta/embed/pipeline.py` — `run_search()`: distinguish connection errors from empty collections
- `carta/tests/test_pipeline.py` — add `run_search` Qdrant-down test
- `carta/install/tests/test_preflight.py` (new) — test OS-aware Docker tip

---

### Task 1: macOS-aware Docker tip in `_check_docker_running()`

**Files:**
- Modify: `carta/install/preflight.py:436-467`
- Create: `carta/install/tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

```python
# carta/install/tests/test_preflight.py
import platform
from unittest.mock import patch
import subprocess
import pytest

from carta.install.preflight import PreflightChecker


class TestDockerRunningTip:
    def _make_checker(self, os_type: str) -> PreflightChecker:
        checker = PreflightChecker(interactive=False)
        checker.os_type = os_type
        return checker

    def test_macos_tip_mentions_docker_desktop_app(self):
        checker = self._make_checker("macos")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "warn"
        assert "Docker Desktop" in result.suggestion or "menu bar" in result.suggestion.lower()

    def test_linux_tip_mentions_systemctl(self):
        checker = self._make_checker("linux")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "warn"
        assert "systemctl" in result.suggestion

    def test_pass_when_docker_running(self):
        checker = self._make_checker("macos")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "pass"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/install/tests/test_preflight.py -v
```

Expected: FAIL — `test_macos_tip_mentions_docker_desktop_app` fails because current suggestion is not OS-specific.

- [ ] **Step 3: Make `_check_docker_running` OS-aware**

In `carta/install/preflight.py`, replace the `_check_docker_running` method's suggestion string (lines ~458, ~464) with an OS-aware call:

```python
def _check_docker_running(self) -> PreflightCheck:
    """Check Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return PreflightCheck(
                name="docker_running",
                status="pass",
                message="Docker daemon running",
                category="infrastructure",
            )
        else:
            return PreflightCheck(
                name="docker_running",
                status="warn",
                message="Docker installed but daemon not running",
                category="infrastructure",
                fixable=False,
                suggestion=self._docker_running_instructions(),
            )
    except Exception as e:
        return PreflightCheck(
            name="docker_running",
            status="skip",
            message=f"Could not check Docker status: {e}",
            category="infrastructure",
        )

def _docker_running_instructions(self) -> str:
    """Return OS-specific instructions for starting Docker daemon."""
    if self.os_type == "macos":
        return (
            "Open the Docker Desktop app first "
            "(look for the whale icon in your menu bar). "
            "On macOS, Docker requires the Desktop app to be running."
        )
    elif self.os_type == "linux":
        return "Run: sudo systemctl start docker"
    elif self.os_type == "windows":
        return "Start Docker Desktop from the Start menu"
    return "Start Docker Desktop (macOS/Windows) or run 'sudo systemctl start docker' (Linux)"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/install/tests/test_preflight.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/install/preflight.py carta/install/tests/test_preflight.py
git commit -m "feat: OS-aware Docker daemon tip in carta doctor"
```

---

### Task 2: Interactive Qdrant start prompt in `carta doctor` (no `--fix` required)

**Files:**
- Modify: `carta/cli.py:217-257`

The current flow only prompts to fix when `--fix` is passed. We want `carta doctor` to always offer to fix fixable issues interactively.

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_cli.py` (find the existing doctor test section or append):

```python
class TestCmdDoctorInteractiveFix:
    """carta doctor prompts to fix Qdrant without --fix flag."""

    def test_prompts_to_fix_when_fixable_failures_exist(self, capsys):
        """When fixable failures exist and --yes not set, AutoInstaller.fix_all is called."""
        from unittest.mock import patch, MagicMock
        import argparse

        args = argparse.Namespace(fix=False, yes=False, verbose=False, json=False)

        mock_result = MagicMock()
        mock_result.fixable_failures = [MagicMock(name="qdrant_running")]
        mock_result.critical_failures = []
        mock_result.can_proceed.return_value = True  # after fix
        mock_result.is_healthy.return_value = True

        with patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller") as MockInstaller:
            mock_checker_instance = MagicMock()
            mock_checker_instance.run.return_value = mock_result
            MockChecker.return_value = mock_checker_instance

            mock_installer_instance = MagicMock()
            mock_installer_instance.fix_all.return_value = {"qdrant_running": True}
            MockInstaller.return_value = mock_installer_instance

            from carta.cli import cmd_doctor
            try:
                cmd_doctor(args)
            except SystemExit:
                pass

            mock_installer_instance.fix_all.assert_called_once_with(mock_result)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/tests/test_cli.py::TestCmdDoctorInteractiveFix -v
```

Expected: FAIL — `fix_all` is not called when `--fix` is not set.

- [ ] **Step 3: Update `cmd_doctor` to always offer interactive fix**

Replace `cmd_doctor` in `carta/cli.py` with:

```python
def cmd_doctor(args):
    """Run diagnostic checks and optionally auto-fix issues."""
    from carta.install.preflight import PreflightChecker, PreflightResult
    from carta.install.auto_fix import AutoInstaller

    interactive = not args.yes  # --yes flag disables prompts
    checker = PreflightChecker(interactive=interactive, verbose=args.verbose)
    result = checker.run()

    # Print report
    if args.json:
        print(result.to_json())
    else:
        result.print_report(verbose=args.verbose)

    # Offer to fix fixable failures (always, not just with --fix)
    should_fix = args.fix  # --fix means auto-confirm; no --fix means prompt
    if result.fixable_failures:
        if not should_fix and not args.json:
            # Ask interactively (AutoInstaller respects interactive=True)
            should_fix = True  # fix_all will prompt per-issue via _prompt_user

        if should_fix:
            if not args.json:
                print(f"\n🔧 Attempting to fix {len(result.fixable_failures)} issue(s)...")
            installer = AutoInstaller(interactive=interactive, verbose=args.verbose)
            fixes = installer.fix_all(result)

            successful = sum(1 for success in fixes.values() if success)
            if not args.json:
                print(f"\n✅ Fixed: {successful}/{len(fixes)}")

            # Re-run checks to verify fixes
            if successful > 0 and not args.json:
                print("\n🔄 Re-running checks to verify fixes...")
                result = checker.run()
                result.print_report(verbose=args.verbose)
    elif not args.json:
        if args.fix:
            print("\n✅ No fixable issues found.")

    # Exit with error code if critical failures remain
    if not result.can_proceed():
        if not args.json:
            installer = AutoInstaller(interactive=False)
            installer.print_setup_guide(result)
        sys.exit(1)

    sys.exit(0)
```

- [ ] **Step 4: Run tests**

```bash
pytest carta/tests/test_cli.py::TestCmdDoctorInteractiveFix -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat: carta doctor prompts to fix Qdrant without --fix flag"
```

---

### Task 3: `carta search` fails clearly when Qdrant is down

**Files:**
- Modify: `carta/embed/pipeline.py:808-812`
- Modify: `carta/tests/test_pipeline.py`

The `except Exception: pass` at line 810 swallows Qdrant connection errors. We need to detect connection failures and surface them as a `RuntimeError` with an actionable message.

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_pipeline.py`:

```python
class TestRunSearch:
    """Tests for run_search error handling."""

    def test_raises_runtime_error_when_qdrant_connection_refused(self):
        """When Qdrant is down, run_search raises RuntimeError with actionable message."""
        import requests
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        # Client constructor succeeds (just warns), but query_points fails
        mock_client = MagicMock()
        mock_client.query_points.side_effect = Exception("Connection refused")

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            with pytest.raises(RuntimeError, match="Qdrant"):
                run_search("test query", cfg)

    def test_returns_empty_when_collection_missing(self):
        """When a collection doesn't exist yet, run_search returns [] without error."""
        from unittest.mock import patch, MagicMock
        from qdrant_client.http.exceptions import UnexpectedResponse
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        mock_client = MagicMock()
        # 404-like response when collection doesn't exist
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"
        mock_response.content = b'{"status": {"error": "Not found"}, "time": 0.0}'
        mock_client.query_points.side_effect = UnexpectedResponse(
            status_code=404,
            reason_phrase="Not Found",
            content=b'{"status": {"error": "Not found"}, "time": 0.0}',
            headers={},
        )

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("test query", cfg)

        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/tests/test_pipeline.py::TestRunSearch -v
```

Expected:
- `test_raises_runtime_error_when_qdrant_connection_refused` — FAIL (currently returns `[]` silently)
- `test_returns_empty_when_collection_missing` — may pass or fail depending on exception type

- [ ] **Step 3: Fix `run_search` to distinguish connection errors**

In `carta/embed/pipeline.py`, import `UnexpectedResponse` at the top of `run_search` and split the exception handling in the per-collection loop:

```python
def run_search(query: str, cfg: dict, verbose: bool = False) -> list[dict]:
    """Search both text and visual collections for results matching query."""
    from carta.search.scoped import get_search_collections
    from qdrant_client import QdrantClient
    from qdrant_client.http.exceptions import UnexpectedResponse
    from pathlib import Path

    top_n = cfg.get("search", {}).get("top_n", 5)
    repo_root = Path(find_config()).parent

    try:
        collections = get_search_collections(cfg, "repo")
    except ValueError:
        collections = [collection_name(cfg, "doc")]
        if cfg.get("embed", {}).get("colpali_enabled", False):
            collections.append(f"{cfg['project_name']}_visual")

    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e

    all_results = []

    for coll_name in collections:
        try:
            if coll_name.endswith("_visual"):
                from carta.embed.colpali import is_colpali_available, ColPaliEmbedder, ColPaliError

                if not is_colpali_available():
                    continue

                embed_cfg = cfg.get("embed", {})
                if not embed_cfg.get("colpali_enabled", False):
                    continue

                model_name = embed_cfg.get("colpali_model", "vidore/colpali-v1.3-hf")
                device = embed_cfg.get("colpali_device", "cpu")

                try:
                    embedder = ColPaliEmbedder(
                        model_name=model_name,
                        device=device,
                        batch_size=1,
                    )
                    query_vectors = embedder.embed_query(query)
                    query_vector_list = query_vectors.tolist() if hasattr(query_vectors, "tolist") else list(query_vectors)

                    response = client.query_points(
                        collection_name=coll_name,
                        query=query_vector_list,
                        using="colpali",
                        limit=top_n,
                        with_payload=True,
                    )

                    for r in response.points:
                        payload = r.payload or {}
                        all_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual",
                        })

                except Exception:
                    continue
            else:
                ollama_url = cfg["embed"]["ollama_url"]
                model = cfg["embed"]["ollama_model"]
                query_vec = get_embedding(query, ollama_url=ollama_url, model=model, prefix="search_query: ")

                response = client.query_points(
                    collection_name=coll_name,
                    query=query_vec,
                    limit=top_n,
                    with_payload=True,
                )

                for r in response.points:
                    payload = r.payload or {}
                    all_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                    })

        except UnexpectedResponse as e:
            # Collection doesn't exist yet — skip it (not an error)
            if e.status_code == 404:
                continue
            # Other HTTP errors from Qdrant may indicate it's down
            raise RuntimeError(
                f"Qdrant error: {e}. "
                "If Qdrant is not running, start it with: carta doctor --fix"
            ) from e
        except Exception as e:
            # Distinguish connection refused / transport errors from other failures
            err_str = str(e).lower()
            if any(kw in err_str for kw in ("connection refused", "connect", "network", "timeout", "unreachable")):
                raise RuntimeError(
                    f"Cannot reach Qdrant — is it running? "
                    f"Start it with: carta doctor --fix\n(Detail: {e})"
                ) from e
            # Other unexpected errors (e.g. bad payload) — skip collection
            continue

    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:top_n]
```

- [ ] **Step 4: Run tests**

```bash
pytest carta/tests/test_pipeline.py::TestRunSearch -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
pytest carta/tests/ carta/install/tests/ -v --tb=short
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "fix: carta search raises clear error when Qdrant is not running"
```

---

### Task 4: Manual smoke test

- [ ] **Step 1: Verify `carta doctor` prompts for Qdrant (if Qdrant is stopped)**

Stop Qdrant if running:
```bash
docker stop qdrant 2>/dev/null || true
```

Run doctor:
```bash
carta doctor
```

Expected: check fails, then prompt appears: `Qdrant is not running. Start it now? [Y/n]:`

- [ ] **Step 2: Verify macOS Docker tip**

With Docker stopped:
```bash
carta doctor
```

Expected: `docker_running` warning shows "Open the Docker Desktop app first (look for the whale icon in your menu bar)."

- [ ] **Step 3: Verify `carta search` error message**

With Qdrant stopped:
```bash
carta search "test query"
```

Expected: clean error to stderr — `Error: Cannot reach Qdrant — is it running? Start it with: carta doctor --fix` — and exit code 1. No qdrant_client UserWarning noise in the output.

- [ ] **Step 4: Final commit if smoke tests passed**

```bash
git add -p  # review any remaining changes
git commit -m "chore: carta doctor + search Qdrant UX improvements"
```
