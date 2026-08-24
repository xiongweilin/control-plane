from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.metrics import ControlPlaneCollector
from control_plane.outward_semantics import INTEGRITY_KINDS, integrity_violation_flags, project_repair
from control_plane.storage import Store


def _row(*, status: str, resolution_kind: str, restoration_status: str, proofs: str = "[]", bases: str = "[]", result: str = "legacy-result") -> dict[str, str]:
    return {"status": status, "resolution_kind": resolution_kind, "restoration_status": restoration_status, "restoration_proof_refs_json": proofs, "resolution_basis_refs_json": bases, "result": result}


def test_normal_projection_matrix_and_no_action_required_stays_generic() -> None:
    cases = [
        (_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs='["proof:1"]', bases='["basis:1"]'), "已验证恢复"),
        (_row(status="closed", resolution_kind="no_action_required", restoration_status="verified", proofs='["proof:2"]', bases='["basis:2"]'), "工作流=closed；处置=no_action_required；恢复判断=verified"),
        (_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]'), "方案被拒绝，处理结束；恢复未验证"),
        (_row(status="rolled_back", resolution_kind="rolled_back", restoration_status="unverified", bases='["basis:rollback"]'), "已执行回滚；目标恢复尚未验证"),
        (_row(status="recovering", resolution_kind="unresolved", restoration_status="unknown"), "现实结果仍未知"),
    ]
    for row, expected in cases:
        projection = project_repair(row)
        assert projection["semantic_summary"] == expected
        assert projection["integrity_violations"] == []


def test_integrity_conflict_never_renders_positive_claim() -> None:
    projection = project_repair(_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs="[]", bases='["basis:restored"]'))
    assert projection["semantic_summary"] == "语义完整性异常，需检查"
    assert projection["integrity_violations"] == ["restored_without_proof"]


def test_malformed_lineage_is_not_evidence_and_legacy_result_is_not_semantics() -> None:
    projection = project_repair(_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs="not-json", bases='["basis:restored"]', result="rejected"))
    assert projection["restoration_proof_refs"] == []
    assert projection["semantic_summary"] == "语义完整性异常，需检查"
    first = project_repair(_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]', result="verified"))
    second = project_repair(_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]', result="different-legacy-text"))
    assert first == second


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(ControlPlaneConfig(), api_key="secret", data_dir=tmp_path / "data", patch_dir=tmp_path / "data" / "patches", evidence_dir=tmp_path / "data" / "evidence", state_db=tmp_path / "data" / "control-plane.db", notification_enabled=False, model_preflight_enabled=False)


def test_status_and_evidence_expose_same_additive_projection(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    store: Store = app.state.store
    store.create_repair("repair-c4b-api", "fp-c4b", "{}")
    store.set_repair_status("repair-c4b-api", "closed", result="legacy-result", finished_at=1)
    store.set_repair_resolution("repair-c4b-api", resolution_kind="rejected", restoration_status="unverified", basis_refs=["basis:reject"])
    client = TestClient(app)
    headers = {"X-Control-Plane-Key": "secret"}
    status_row = next(row for row in client.get("/status", headers=headers).json()["recent_repairs"] if row["id"] == "repair-c4b-api")
    evidence_row = next(row for row in client.get("/v1/evidence", headers=headers).json()["repairs"] if row["id"] == "repair-c4b-api")
    semantic_keys = {"status", "resolution_kind", "restoration_status", "semantic_summary", "restoration_proof_refs", "resolution_basis_refs", "integrity_violations"}
    assert {key: status_row[key] for key in semantic_keys} == {key: evidence_row[key] for key in semantic_keys}
    assert status_row["result"] == "legacy-result"
    assert evidence_row["result"] == "legacy-result"


def test_metrics_snapshot_and_fixed_integrity_counts(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    for repair_id in ("r-closed-unverified", "r-restored-no-proof", "r-no-basis"):
        store.create_repair(repair_id, repair_id, "{}")
    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='restored', restoration_status='unverified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[\"basis:1\"]' WHERE id='r-closed-unverified'")  # noqa: SLF001
    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='restored', restoration_status='verified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[\"basis:2\"]' WHERE id='r-restored-no-proof'")  # noqa: SLF001
    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='rejected', restoration_status='unverified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[]' WHERE id='r-no-basis'")  # noqa: SLF001
    families = {family.name: family for family in ControlPlaneCollector(store, lambda: 5).collect()}
    resolution = {sample.labels["resolution_kind"]: sample.value for sample in families["control_plane_repairs_by_resolution"].samples}
    restoration = {sample.labels["restoration_status"]: sample.value for sample in families["control_plane_repairs_by_restoration"].samples}
    integrity = {sample.labels["kind"]: sample.value for sample in families["control_plane_semantic_integrity_violations"].samples}
    assert resolution == {"rejected": 1, "restored": 2}
    assert restoration == {"unverified": 2, "verified": 1}
    assert integrity == {"closed_restored_unverified": 1, "restored_without_proof": 2, "non_unresolved_without_basis": 1}
    assert set(integrity) == set(INTEGRITY_KINDS)
    store.close()


def test_terminal_notifications_use_fresh_post_authority_projection() -> None:
    source = Path("src/control_plane/service.py").read_text(encoding="utf-8-sig")
    assert "def _fresh_outward_projection" in source
    assert "row = self.store.get_repair(repair_id)" in source
    assert "return project_repair(row)" in source
    for authority_call in ("self.closure_authority.close_rejected(repair_id)", "self.closure_authority.record_rolled_back(repair_id, receipts)", "self.closure_authority.close_restored(repair_id)"):
        index = source.index(authority_call)
        assert index < source.index("_fresh_outward_projection(repair_id)", index)
    assert 'await self._notify("info", "修复已被拒绝"' not in source
    assert 'await self._notify("warning", "修复已回滚"' not in source
    assert 'await self._notify("info", "验证通过"' not in source
    assert 'f"修复完成：' not in source


def test_outward_layer_has_no_authority_or_mutation_seam() -> None:
    source = Path("src/control_plane/outward_semantics.py").read_text(encoding="utf-8-sig")
    assert "ClosureAuthority" not in source
    assert "from .storage import Store" not in source
    assert "set_repair_" not in source
    assert "UPDATE repairs" not in source
    assert "INSERT INTO" not in source


def test_integrity_flags_are_fixed_and_observational() -> None:
    assert integrity_violation_flags(_row(status="closed", resolution_kind="restored", restoration_status="unverified", proofs="[]", bases="[]")) == {"closed_restored_unverified": True, "restored_without_proof": True, "non_unresolved_without_basis": True}
