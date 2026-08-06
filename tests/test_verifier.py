from __future__ import annotations

from control_plane.verifier import Verifier


def test_diff_allowed_rejects_forbidden_paths() -> None:
    ok, _ = Verifier.diff_allowed("/srv/stack/x", "docs/readme.md | 1 +")
    assert ok
    denied, _ = Verifier.diff_allowed("/srv/stack/x", "prometheus.yml | 2 +-")
    assert not denied
    denied, _ = Verifier.diff_allowed("/srv/stack/x", "verifier.py | 3 ++-")
    assert not denied
