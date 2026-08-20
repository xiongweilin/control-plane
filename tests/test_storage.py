from __future__ import annotations

import time

from control_plane.storage import Store


def _make_candidate(store: Store, candidate_id: str, deadline: int) -> None:
    store.create_candidate(
        candidate_id,
        "HighCPU|dify|*",
        "control-plane",
        '[{"tool":"restart_service"}]',
        "container_status",
        "candidate",
        deadline,
        "archive",
        "",
        "",
        "repair-1",
    )


def test_candidate_lifecycle(tmp_path) -> None:
    store = Store(tmp_path / "state.db")
    _make_candidate(store, "cand-1", int(time.time()) + 1000)
    assert store.find_candidate("HighCPU|dify|*", ("candidate",)) is not None
    store.promote_candidate("cand-1")
    assert store.get_candidate("cand-1")["status"] == "official"
    assert len(store.list_playbooks()) == 1
    store.close()


def test_candidate_expiration(tmp_path) -> None:
    store = Store(tmp_path / "state.db")
    _make_candidate(store, "cand-old", int(time.time()) - 10)
    _make_candidate(store, "cand-new", int(time.time()) + 10)
    assert store.expire_candidates(int(time.time())) == 1
    assert store.get_candidate("cand-old")["status"] == "archived"
    assert store.get_candidate("cand-new")["status"] == "candidate"
    store.close()
