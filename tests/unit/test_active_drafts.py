"""ActiveDraftStore restart-pointer persistence (Issue #48, Phase 3)."""

from utils.active_drafts import ActiveDraftStore


def test_missing_file_loads_empty(tmp_path):
    store = ActiveDraftStore(str(tmp_path / "sub" / "active.json"))
    assert store.load() == {}


def test_save_and_load_roundtrip(tmp_path):
    store = ActiveDraftStore(str(tmp_path / "active.json"))
    store.save(111, "draft-a")
    store.save(222, "draft-b")
    assert store.load() == {"111": "draft-a", "222": "draft-b"}


def test_remove_is_idempotent(tmp_path):
    store = ActiveDraftStore(str(tmp_path / "active.json"))
    store.save(111, "draft-a")
    store.remove(111)
    store.remove(111)  # no error when already absent
    assert store.load() == {}


def test_corrupt_file_loads_empty(tmp_path):
    path = tmp_path / "active.json"
    path.write_text("{not valid json")
    assert ActiveDraftStore(str(path)).load() == {}


def test_clear_removes_the_file(tmp_path):
    store = ActiveDraftStore(str(tmp_path / "active.json"))
    store.save(111, "draft-a")
    store.clear()
    assert store.load() == {}
    assert not (tmp_path / "active.json").exists()


def test_clear_is_idempotent_when_file_absent(tmp_path):
    store = ActiveDraftStore(str(tmp_path / "sub" / "active.json"))
    store.clear()  # no error when nothing to remove
    store.clear()
