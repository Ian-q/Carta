import json
import os
import socket

from carta.embed.status import StatusWriter, STATUS_FILENAME


def _read(repo_root):
    p = repo_root / ".carta" / STATUS_FILENAME
    return json.loads(p.read_text())


def test_start_writes_running_with_identity(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=3)
    data = _read(tmp_path)
    assert data["schema"] == 1
    assert data["phase"] == "running"
    assert data["total"] == 3
    assert data["pid"] == os.getpid()
    assert data["host"] == socket.gethostname()
    assert data["embedded"] == 0 and data["chunks"] == 0
    assert data["finished_at"] is None


def test_file_start_sets_current(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=2)
    sw.file_start(1, "big.pdf")
    data = _read(tmp_path)
    assert data["current_idx"] == 1
    assert data["current_file"] == "big.pdf"
    assert isinstance(data["current_file_started_at"], float)


def test_file_done_accumulates_counters(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=2)
    sw.file_start(1, "a.md")
    sw.file_done(embedded=1, chunks=10)
    sw.file_start(2, "b.md")
    sw.file_done(skipped=1)
    data = _read(tmp_path)
    assert data["embedded"] == 1
    assert data["skipped"] == 1
    assert data["chunks"] == 10


def test_finish_sets_phase_and_finished_at(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)
    sw.finish("done")
    data = _read(tmp_path)
    assert data["phase"] == "done"
    assert isinstance(data["finished_at"], float)
    assert data["current_file"] is None


def test_disabled_writes_nothing(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=False)
    sw.start(total=1)
    sw.file_start(1, "x.md")
    sw.finish("done")
    assert not (tmp_path / ".carta" / STATUS_FILENAME).exists()


def test_write_failure_is_swallowed(tmp_path):
    # .carta missing -> parent dir doesn't exist; must not raise
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)  # should not raise even though .carta/ is absent
    sw.finish("done")


def test_no_tmp_file_left_behind(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)
    sw.finish("done")
    leftovers = list((tmp_path / ".carta").glob("*.tmp"))
    assert leftovers == []
