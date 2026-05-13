from utils import match_ids


def test_next_match_id_increments_highest_valid_id():
    assert match_ids.next_match_id(["GF-0001", "GF-0007", "bad", "GF-ABCD"]) == "GF-0008"


def test_reserve_match_id_persists_counter(tmp_path):
    path = tmp_path / "match_ids.json"

    assert match_ids.reserve_match_id(path) == "GF-0001"
    assert match_ids.reserve_match_id(path) == "GF-0002"
