from pathlib import Path

import pytest

from app.database import Store
from app.errors import Error
from app.models import Run


def test_concurrent_run_is_rejected_and_history_survives_restart(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    store.open()
    first = Run(id="one", operation="warm", query_name="articles", target_pops=["LHR", "BOM"])
    store.start_run(first)
    with pytest.raises(Error):
        store.start_run(Run(id="two", operation="extract_ip"))
    store.close()
    store.open()
    interrupted = store.get_run("one")
    assert interrupted.status == "interrupted"
    assert interrupted.missing_pops == ["BOM", "LHR"]
    store.start_run(Run(id="two", operation="extract_ip"))
    assert store.healthy()
    store.close()


def test_second_process_cannot_own_same_state(tmp_path: Path):
    a, b = Store(tmp_path / "state.db"), Store(tmp_path / "state.db")
    a.open()
    try:
        with pytest.raises(RuntimeError, match="one instance"):
            b.open()
    finally:
        a.close()


def test_no_targets_cannot_be_marked_complete():
    run = Run(id="one", operation="extract_ip")
    run.finish_coverage(set())
    assert run.status == "incomplete"


def test_unknown_pop_does_not_reduce_coverage():
    run = Run(id="one", operation="warm", target_pops=["BOM", "LHR"])
    run.finish_coverage({"BOM", "SIN"})
    assert run.status == "incomplete" and run.missing_pops == ["LHR"]


def test_late_checkpoint_cannot_resurrect_terminal_run(tmp_path):
    store = Store(tmp_path / "state.db")
    store.open()
    try:
        checkpoint = Run(id="one", operation="extract_ip")
        store.start_run(checkpoint)
        finished = checkpoint.model_copy(deep=True)
        finished.status = "interrupted"
        store.save_run(finished)
        store.save_run(checkpoint)
        assert store.get_run("one").status == "interrupted"
        store.start_run(Run(id="two", operation="extract_ip"))
    finally:
        store.close()


def test_failed_open_releases_process_lock(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    def fail():
        raise RuntimeError("initialization failed")
    monkeypatch.setattr(store, "_initialize", fail)
    with pytest.raises(RuntimeError, match="initialization"):
        store.open()
    other = Store(store.path)
    other.open()
    other.close()
