import subprocess
import sys
from pathlib import Path
import os
import shutil

from carta.install.bootstrap import CARTA_RUNTIME_SRC

def run_carta(args: list[str], cwd: Path = None) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) if not existing else f"{repo_root}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-m", "carta.cli"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

def test_version():
    result = run_carta(["--version"])
    assert result.returncode == 0
    from carta import __version__
    assert __version__ in result.stdout

def test_unknown_command_exits_nonzero():
    result = run_carta(["notacommand"])
    assert result.returncode != 0

def test_scan_requires_config(tmp_path):
    result = run_carta(["scan"], cwd=tmp_path)
    assert result.returncode != 0
    assert "config" in result.stderr.lower() or "config" in result.stdout.lower()


def test_runtime_cli_direct_execution(tmp_path):
    # Simulate what `carta init` does: copy the runtime into `.carta/carta`.
    # Then run the runtime's CLI via `python .carta/carta/cli.py ...`.
    dest = tmp_path / ".carta" / "carta"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CARTA_RUNTIME_SRC, dest)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(dest / "cli.py"), "--version"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, result.stderr


class TestCmdDoctorInteractiveFix:
    """carta doctor prompts to fix Qdrant without --fix flag."""

    def test_calls_fix_all_when_fixable_failures_exist_without_fix_flag(self):
        """When fixable failures exist, fix_all is called even without --fix."""
        import argparse
        from unittest.mock import patch, MagicMock, call
        from carta.cli import cmd_doctor

        args = argparse.Namespace(fix=False, yes=False, verbose=False, json=False)

        mock_result = MagicMock()
        mock_result.fixable_failures = [MagicMock()]
        mock_result.critical_failures = []
        mock_result.can_proceed.return_value = True
        mock_result.is_healthy.return_value = True
        mock_result.warnings = []

        with patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller") as MockInstaller:
            mock_checker_instance = MagicMock()
            mock_checker_instance.run.return_value = mock_result
            MockChecker.return_value = mock_checker_instance

            mock_installer_instance = MagicMock()
            mock_installer_instance.fix_all.return_value = {"qdrant_running": True}
            MockInstaller.return_value = mock_installer_instance

            try:
                cmd_doctor(args)
            except SystemExit:
                pass

            mock_installer_instance.fix_all.assert_called_once_with(mock_result)

    def test_does_not_call_fix_all_when_no_fixable_failures(self):
        """When no fixable failures, fix_all is not called."""
        import argparse
        from unittest.mock import patch, MagicMock
        from carta.cli import cmd_doctor

        args = argparse.Namespace(fix=False, yes=False, verbose=False, json=False)

        mock_result = MagicMock()
        mock_result.fixable_failures = []
        mock_result.critical_failures = []
        mock_result.can_proceed.return_value = True
        mock_result.is_healthy.return_value = True
        mock_result.warnings = []

        with patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller") as MockInstaller:
            mock_checker_instance = MagicMock()
            mock_checker_instance.run.return_value = mock_result
            MockChecker.return_value = mock_checker_instance

            mock_installer_instance = MagicMock()
            MockInstaller.return_value = mock_installer_instance

            try:
                cmd_doctor(args)
            except SystemExit:
                pass

            mock_installer_instance.fix_all.assert_not_called()


# ---------------------------------------------------------------------------
# RAM-aware vision_workers tuning
# ---------------------------------------------------------------------------

class TestRecommendVisionWorkers:
    def test_recommends_1_for_low_ram(self):
        from carta.cli import _recommend_vision_workers
        assert _recommend_vision_workers(8) == 1
        assert _recommend_vision_workers(16) == 1
        assert _recommend_vision_workers(17) == 1

    def test_recommends_2_for_36gb(self):
        """36GB Mac: 2 workers fit comfortably alongside OCR + nomic."""
        from carta.cli import _recommend_vision_workers
        assert _recommend_vision_workers(36) == 2

    def test_recommends_more_with_more_ram(self):
        from carta.cli import _recommend_vision_workers
        assert _recommend_vision_workers(48) == 3
        assert _recommend_vision_workers(64) == 4

    def test_caps_at_4_for_huge_ram(self):
        from carta.cli import _recommend_vision_workers
        assert _recommend_vision_workers(128) == 4
        assert _recommend_vision_workers(512) == 4


class TestMaybeTuneWorkers:
    def test_skips_when_skip_flag_true(self, monkeypatch):
        from carta.cli import _maybe_tune_workers
        cfg = {"embed": {"vision_workers": 4}}
        out = _maybe_tune_workers(cfg, skip=True)
        assert out["embed"]["vision_workers"] == 4

    def test_skips_when_env_var_set(self, monkeypatch):
        from carta.cli import _maybe_tune_workers
        monkeypatch.setenv("CARTA_NO_TUNE", "1")
        cfg = {"embed": {"vision_workers": 4}}
        out = _maybe_tune_workers(cfg, skip=False)
        assert out["embed"]["vision_workers"] == 4

    def test_skips_when_not_tty(self, monkeypatch):
        from carta.cli import _maybe_tune_workers
        # Capsys / pytest already redirects stdin/stdout away from the tty.
        cfg = {"embed": {"vision_workers": 4}}
        out = _maybe_tune_workers(cfg, skip=False)
        assert out["embed"]["vision_workers"] == 4


def test_statusline_print_segment_smoke(tmp_path, monkeypatch, capsys):
    """`carta statusline` (no flags) prints the segment for cwd, never errors."""
    import io, json, os, socket
    import pytest
    from carta import cli

    (tmp_path / ".carta").mkdir()
    status = {
        "schema": 1, "phase": "running", "host": socket.gethostname(),
        "pid": os.getpid(), "total": 5, "current_idx": 2,
        "current_file": "x.md", "current_file_started_at": 0.0,
        "updated_at": 0.0, "finished_at": None, "embedded": 1,
        "skipped": 0, "errors": 0, "chunks": 3,
    }
    (tmp_path / ".carta" / "embed-status.json").write_text(json.dumps(status))
    monkeypatch.setattr(__import__("sys"), "stdin",
                        io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    args = type("A", (), {"install": False, "uninstall": False})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_statusline(args)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "carta 2/5" in out.replace("\x1b", "")  # ANSI-tolerant


class TestCmdEvalRerankAssertion:
    """cmd_eval reports rerank-applied counts and hard-fails when rerank was
    requested but silently failed open on every query (the 0.8.0 bug class)."""

    def _eval_yaml(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text(
            'queries:\n'
            '  - q: "alpha"\n'
            '    expect: ["a.md"]\n'
            '  - q: "beta"\n'
            '    expect: ["b.md"]\n'
        )
        return p

    def _run(self, tmp_path, rerank_enabled, applied_per_query):
        """Run cmd_eval with run_search mocked to report the given per-query
        rerank_applied values. Returns the SystemExit code or None."""
        import argparse
        from unittest.mock import patch
        from carta.cli import cmd_eval

        cfg = {
            "project_name": "p",
            "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "m"},
            "search": {"top_n": 5, "rerank": {"enabled": rerank_enabled}},
        }
        applied_iter = iter(applied_per_query)

        def fake_run_search(query, c, verbose=False, stats=None):
            if stats is not None:
                stats["rerank_requested"] = rerank_enabled
                stats["rerank_applied"] = next(applied_iter)
            return [{"score": 0.9, "source": "docs/a.md", "excerpt": "x", "type": "text"}]

        args = argparse.Namespace(eval_path=str(self._eval_yaml(tmp_path)), k=5)
        with patch("carta.cli.find_config", return_value=Path("/fake/.carta/config.yaml")), \
             patch("carta.config.load_config", return_value=cfg), \
             patch("carta.embed.pipeline.run_search", side_effect=fake_run_search):
            try:
                cmd_eval(args)
            except SystemExit as e:
                return e.code
        return None

    def test_zero_applied_with_rerank_requested_exits_nonzero(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[False, False])
        captured = capsys.readouterr()
        assert code == 1, "silent fail-open on every query must hard-fail the eval"
        assert "failing open" in captured.err
        assert "applied on 0/2 queries" in captured.out

    def test_partial_applied_reports_count_and_passes(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[True, False])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: applied on 1/2 queries" in captured.out

    def test_all_applied_reports_count(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[True, True])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: applied on 2/2 queries" in captured.out

    def test_rerank_not_requested_reports_and_passes(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=False, applied_per_query=[False, False])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: not requested" in captured.out


class TestCmdRemember:
    def _args(self, **kw):
        import argparse
        kw.setdefault("text", "the bench PSU must be on")
        kw.setdefault("type", "quirk")
        kw.setdefault("title", "")
        kw.setdefault("tags", "")
        return argparse.Namespace(**kw)

    def _run(self, args, capture_result=None, capture_error=None):
        from unittest.mock import patch
        from carta.cli import cmd_remember
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        kwargs = {}
        if capture_error:
            kwargs["side_effect"] = capture_error
        else:
            kwargs["return_value"] = capture_result or {
                "path": "docs/quirks/2026-06-11-x.md", "collection": "p_notes", "chunks": 2}
        with patch("carta.cli.find_config", return_value=Path("/fake/.carta/config.yaml")), \
             patch("carta.config.load_config", return_value=cfg), \
             patch("carta.memory.capture.capture_note", **kwargs) as cap:
            try:
                cmd_remember(args)
            except SystemExit as e:
                return e.code, cap
        return None, cap

    def test_happy_path_prints_path_and_collection(self, capsys):
        code, cap = self._run(self._args(tags="bench, can"))
        out = capsys.readouterr().out
        assert code is None
        assert "docs/quirks/2026-06-11-x.md" in out
        assert "p_notes" in out
        # comma-string tags become a list
        assert cap.call_args.kwargs["tags"] == ["bench", "can"]

    def test_no_tags_passes_none(self):
        code, cap = self._run(self._args(tags=""))
        assert cap.call_args.kwargs["tags"] is None

    def test_capture_error_exits_1(self, capsys):
        code, _ = self._run(self._args(), capture_error=ValueError("bad"))
        assert code == 1
        assert "bad" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_doctor — Corpus integrity section (Task 6)
# ---------------------------------------------------------------------------

class TestDoctorCorpusIntegrity:
    """cmd_doctor wires in a read-only corpus-integrity section."""

    _ISSUES_REPORT = {
        "slug_collisions": {"readme": ["docs/a/README.md", "docs/b/README.md"]},
        "empty_files": ["docs/scan.pdf"],
        "partial_empty_files": {},
        "count_mismatches": {},
        "stuck_stale": [],
        "affected_files": ["docs/a/README.md", "docs/b/README.md", "docs/scan.pdf"],
    }

    _CLEAN_REPORT = {
        "slug_collisions": {},
        "empty_files": [],
        "partial_empty_files": {},
        "count_mismatches": {},
        "stuck_stale": [],
        "affected_files": [],
    }

    def _make_args(self, json_flag=False):
        from unittest.mock import MagicMock
        args = MagicMock()
        args.json = json_flag
        args.fix = False
        args.yes = True
        args.verbose = False
        return args

    def _make_preflight_mock(self, MockChecker):
        from unittest.mock import MagicMock
        result = MagicMock()
        result.fixable_failures = []
        result.critical_failures = []
        result.can_proceed.return_value = True
        result.is_healthy.return_value = True
        result.warnings = []
        instance = MagicMock()
        instance.run.return_value = result
        MockChecker.return_value = instance
        return result

    def test_doctor_prints_integrity_section_inside_project(self, tmp_path, capsys):
        from unittest.mock import patch, MagicMock
        from carta import cli

        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            "project_name: test\nqdrant_url: http://localhost:6333\n"
        )

        with patch("carta.cli.find_config", return_value=cfg_file), \
             patch("carta.embed.integrity.scan_corpus_integrity",
                   return_value=self._ISSUES_REPORT) as mock_scan, \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            self._make_preflight_mock(MockChecker)
            try:
                cli.cmd_doctor(self._make_args())
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "Corpus integrity" in out
        assert "readme" in out
        assert "docs/scan.pdf" in out
        assert "carta embed --repair" in out

    def test_doctor_integrity_clean(self, tmp_path, capsys):
        from unittest.mock import patch
        from carta import cli

        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            "project_name: test\nqdrant_url: http://localhost:6333\n"
        )

        with patch("carta.cli.find_config", return_value=cfg_file), \
             patch("carta.embed.integrity.scan_corpus_integrity",
                   return_value=self._CLEAN_REPORT), \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            self._make_preflight_mock(MockChecker)
            try:
                cli.cmd_doctor(self._make_args())
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "Corpus integrity" in out
        assert "no issues found" in out
        assert "carta embed --repair" not in out

    def test_doctor_outside_project(self, capsys):
        """find_config raises FileNotFoundError — doctor finishes, no integrity section."""
        from unittest.mock import patch
        from carta import cli

        with patch("carta.cli.find_config",
                   side_effect=FileNotFoundError("no config")), \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            self._make_preflight_mock(MockChecker)
            try:
                cli.cmd_doctor(self._make_args())
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "Corpus integrity" not in out

    def test_doctor_integrity_scan_error(self, tmp_path, capsys):
        """scan_corpus_integrity raises — doctor prints 'check skipped' and does not crash."""
        from unittest.mock import patch
        from carta import cli

        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            "project_name: test\nqdrant_url: http://localhost:6333\n"
        )

        with patch("carta.cli.find_config", return_value=cfg_file), \
             patch("carta.embed.integrity.scan_corpus_integrity",
                   side_effect=RuntimeError("qdrant unreachable")), \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            self._make_preflight_mock(MockChecker)
            try:
                cli.cmd_doctor(self._make_args())
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "check skipped" in out

    def test_doctor_json_includes_corpus_integrity(self, tmp_path, capsys):
        """With --json, corpus_integrity key is merged into the single JSON document."""
        import json
        from unittest.mock import patch, MagicMock
        from carta import cli

        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            "project_name: test\nqdrant_url: http://localhost:6333\n"
        )

        preflight_dict = {
            "status": "healthy",
            "can_proceed": True,
            "summary": {"total": 1, "passed": 1, "failed": 0,
                        "warnings": 0, "skipped": 0, "fixable": 0},
            "checks": [],
        }

        with patch("carta.cli.find_config", return_value=cfg_file), \
             patch("carta.embed.integrity.scan_corpus_integrity",
                   return_value=self._ISSUES_REPORT), \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            mock_result = MagicMock()
            mock_result.fixable_failures = []
            mock_result.critical_failures = []
            mock_result.can_proceed.return_value = True
            mock_result.is_healthy.return_value = True
            mock_result.warnings = []
            mock_result.to_dict.return_value = preflight_dict
            mock_result.to_json.return_value = json.dumps(preflight_dict)
            instance = MagicMock()
            instance.run.return_value = mock_result
            MockChecker.return_value = instance
            try:
                cli.cmd_doctor(self._make_args(json_flag=True))
            except SystemExit:
                pass

        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "corpus_integrity" in parsed
        assert "slug_collisions" in parsed["corpus_integrity"]
        assert "readme" in parsed["corpus_integrity"]["slug_collisions"]


class TestDoctorIntegrityJsonScanError:
    """JSON mode must emit exactly one valid JSON document even when the
    integrity scan fails — empty stdout would break consumers."""

    def test_json_scan_error_still_emits_json(self, tmp_path, capsys):
        import json
        from unittest.mock import MagicMock, patch
        from carta import cli

        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            "project_name: test\nqdrant_url: http://localhost:6333\n"
        )

        with patch("carta.cli.find_config", return_value=cfg_file), \
             patch("carta.embed.integrity.scan_corpus_integrity",
                   side_effect=RuntimeError("qdrant unreachable")), \
             patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller"):
            result = MagicMock()
            result.fixable_failures = []
            result.critical_failures = []
            result.can_proceed.return_value = True
            result.is_healthy.return_value = True
            result.warnings = []
            result.to_dict.return_value = {"checks": []}
            MockChecker.return_value.run.return_value = result

            args = MagicMock()
            args.json = True
            args.fix = False
            args.yes = True
            args.verbose = False
            try:
                cli.cmd_doctor(args)
            except SystemExit:
                pass

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["corpus_integrity"] == {"skipped": "qdrant unreachable"}
