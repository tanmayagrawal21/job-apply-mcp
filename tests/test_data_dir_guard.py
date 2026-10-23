"""The session-end real-data-dir guard ignores foreign processes' files but still catches our own (#64)."""
import conftest


def test_foreign_entry_does_not_trip_guard():
    assert conftest._own_new_entries(["a"], ["a", "live-server.log"], touched=set()) == []


def test_own_entry_trips_guard():
    assert conftest._own_new_entries(["a"], ["a", "mine.db"], touched={"mine.db"}) == ["mine.db"]


def test_preexisting_touched_entry_is_not_new():
    assert conftest._own_new_entries(["a"], ["a"], touched={"a"}) == []


def test_touch_tracking_records_top_level_name(monkeypatch, tmp_path):
    monkeypatch.setattr(conftest, "_REAL_DATA_DIR", tmp_path)
    monkeypatch.setattr(conftest, "_TOUCHED", set())
    conftest._note_touch(tmp_path / "audit" / "x.jsonl")
    conftest._note_touch(tmp_path.parent / "elsewhere")
    assert conftest._TOUCHED == {"audit"}
