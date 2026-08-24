from __future__ import annotations

from pathlib import Path


def replace(path: str, old: str, new: str, count: int | None = 1) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8-sig")
    if old not in text:
        raise SystemExit(f"missing C4b anchor in {path}: {old[:180]!r}")
    text = text.replace(old, new, -1 if count is None else count)
    file_path.write_text(text, encoding="utf-8")


def main() -> None:
    app = "src/control_plane/app.py"
    replace(
        app,
        "from .notify import Notifier\n",
        "from .notify import Notifier\nfrom .outward_semantics import project_repair\n",
    )
    replace(
        app,
        '''                {\n                    "id": row["id"],\n                    "status": row["status"],\n                    "attempt": row["attempt"],\n                    "result": row["result"],\n                }\n                for row in store.list_repairs_with_fallback(limit=20)\n''',
        '''                {\n                    "id": row["id"],\n                    "status": row["status"],\n                    "attempt": row["attempt"],\n                    "result": row["result"],\n                    **project_repair(row),\n                }\n                for row in store.list_repairs_with_fallback(limit=20)\n''',
    )
    replace(
        app,
        '''                {\n                    "id": row["id"],\n                    "fingerprint": row["fingerprint"],\n                    "status": row["status"],\n                    "attempt": row["attempt"],\n                    "created_at": row["created_at"],\n                    "updated_at": row["updated_at"],\n                    "result": row["result"],\n                }\n                for row in store.list_repairs_with_fallback(limit=20)\n''',
        '''                {\n                    "id": row["id"],\n                    "fingerprint": row["fingerprint"],\n                    "status": row["status"],\n                    "attempt": row["attempt"],\n                    "created_at": row["created_at"],\n                    "updated_at": row["updated_at"],\n                    "result": row["result"],\n                    **project_repair(row),\n                }\n                for row in store.list_repairs_with_fallback(limit=20)\n''',
    )

    service = "src/control_plane/service.py"
    replace(
        service,
        "from .notify import Notifier\n",
        "from .notify import Notifier\nfrom .outward_semantics import project_repair\n",
    )
    replace(
        service,
        '''    async def _resolve_git_version_for_authority(self, repo: str) -> str:\n''',
        '''    def _fresh_outward_projection(self, repair_id: str) -> dict[str, Any]:\n        row = self.store.get_repair(repair_id)\n        if row is None:\n            raise RuntimeError(f"repair missing after terminal authority write: {repair_id}")\n        return project_repair(row)\n\n    async def _resolve_git_version_for_authority(self, repo: str) -> str:\n''',
    )
    replace(
        service,
        '''            self.closure_authority.close_rejected(repair_id)\n            await self._notify("info", "修复已被拒绝", f"repair_id={repair_id}")\n''',
        '''            self.closure_authority.close_rejected(repair_id)\n            projection = self._fresh_outward_projection(repair_id)\n            await self._notify("info", projection["semantic_summary"], f"repair_id={repair_id}")\n''',
    )
    replace(
        service,
        '''            self.closure_authority.record_rolled_back(repair_id, receipts)\n            await self._notify("warning", "修复已回滚", f"repair_id={repair_id}")\n''',
        '''            self.closure_authority.record_rolled_back(repair_id, receipts)\n            projection = self._fresh_outward_projection(repair_id)\n            await self._notify("warning", projection["semantic_summary"], f"repair_id={repair_id}")\n''',
    )
    replace(
        service,
        '''        self.closure_authority.close_restored(repair_id)\n\n        branch = proposal.get("branch")\n''',
        '''        self.closure_authority.close_restored(repair_id)\n        terminal_projection = self._fresh_outward_projection(repair_id)\n\n        branch = proposal.get("branch")\n''',
    )
    replace(
        service,
        '''        try:\n            await self._notify("info", "验证通过", f"repair_id={repair_id}\\n{report.summary}")\n            await self._notify(\n                "info",\n                f"修复完成：{alert.labels.get('alertname', 'unknown')}",\n                f"repair_id={repair_id}\\n{report.summary}{rollback}",\n            )\n        except Exception:\n''',
        '''        try:\n            await self._notify(\n                "info",\n                terminal_projection["semantic_summary"],\n                f"repair_id={repair_id}\\n{report.summary}{rollback}",\n            )\n        except Exception:\n''',
    )

    metrics = "src/control_plane/metrics.py"
    replace(
        metrics,
        "from .runtime import current_run_id\n",
        "from .outward_semantics import INTEGRITY_KINDS, integrity_violation_flags\nfrom .runtime import current_run_id\n",
    )
    replace(
        metrics,
        '''        status_counts: dict[str, int] = {}\n        active = 0\n        recoverable: dict[str, int] = {}\n        for row in rows:\n            status = row["status"]\n            status_counts[status] = status_counts.get(status, 0) + 1\n            if status not in quiescent:\n                active += 1\n            elif status in {s.value for s in RECOVERABLE_STATES}:\n                recoverable[status] = recoverable.get(status, 0) + 1\n''',
        '''        status_counts: dict[str, int] = {}\n        resolution_counts: dict[str, int] = {}\n        restoration_counts: dict[str, int] = {}\n        integrity_counts = {kind: 0 for kind in INTEGRITY_KINDS}\n        active = 0\n        recoverable: dict[str, int] = {}\n        for row in rows:\n            status = row["status"]\n            status_counts[status] = status_counts.get(status, 0) + 1\n            resolution = str(row["resolution_kind"] or "unresolved")\n            restoration = str(row["restoration_status"] or "unverified")\n            resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1\n            restoration_counts[restoration] = restoration_counts.get(restoration, 0) + 1\n            for kind, violated in integrity_violation_flags(row).items():\n                if violated:\n                    integrity_counts[kind] += 1\n            if status not in quiescent:\n                active += 1\n            elif status in {s.value for s in RECOVERABLE_STATES}:\n                recoverable[status] = recoverable.get(status, 0) + 1\n''',
    )
    replace(
        metrics,
        '''        yield recoverable_gauge\n        yield GaugeMetricFamily(\n            "control_plane_candidates",\n''',
        '''        yield recoverable_gauge\n\n        by_resolution = GaugeMetricFamily(\n            "control_plane_repairs_by_resolution",\n            "Control-plane repair snapshot by persisted resolution kind",\n            labels=["resolution_kind"],\n        )\n        for resolution, count in sorted(resolution_counts.items()):\n            by_resolution.add_metric([resolution], count)\n        yield by_resolution\n\n        by_restoration = GaugeMetricFamily(\n            "control_plane_repairs_by_restoration",\n            "Control-plane repair snapshot by persisted restoration status",\n            labels=["restoration_status"],\n        )\n        for restoration, count in sorted(restoration_counts.items()):\n            by_restoration.add_metric([restoration], count)\n        yield by_restoration\n\n        semantic_integrity = GaugeMetricFamily(\n            "control_plane_semantic_integrity_violations",\n            "Current repair rows violating fixed outward semantic invariants",\n            labels=["kind"],\n        )\n        for kind in INTEGRITY_KINDS:\n            semantic_integrity.add_metric([kind], integrity_counts[kind])\n        yield semantic_integrity\n\n        yield GaugeMetricFamily(\n            "control_plane_candidates",\n''',
    )

    Path("tests/test_c4b_outward_semantics.py").write_text(
        '''from __future__ import annotations\n\nfrom dataclasses import replace\nfrom pathlib import Path\n\nfrom fastapi.testclient import TestClient\n\nfrom control_plane.app import create_app\nfrom control_plane.config import ControlPlaneConfig\nfrom control_plane.metrics import ControlPlaneCollector\nfrom control_plane.outward_semantics import INTEGRITY_KINDS, integrity_violation_flags, project_repair\nfrom control_plane.storage import Store\n\n\ndef _row(*, status: str, resolution_kind: str, restoration_status: str, proofs: str = "[]", bases: str = "[]", result: str = "legacy-result") -> dict[str, str]:\n    return {"status": status, "resolution_kind": resolution_kind, "restoration_status": restoration_status, "restoration_proof_refs_json": proofs, "resolution_basis_refs_json": bases, "result": result}\n\n\ndef test_normal_projection_matrix_and_no_action_required_stays_generic() -> None:\n    cases = [\n        (_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs='["proof:1"]', bases='["basis:1"]'), "已验证恢复"),\n        (_row(status="closed", resolution_kind="no_action_required", restoration_status="verified", proofs='["proof:2"]', bases='["basis:2"]'), "工作流=closed；处置=no_action_required；恢复判断=verified"),\n        (_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]'), "方案被拒绝，处理结束；恢复未验证"),\n        (_row(status="rolled_back", resolution_kind="rolled_back", restoration_status="unverified", bases='["basis:rollback"]'), "已执行回滚；目标恢复尚未验证"),\n        (_row(status="recovering", resolution_kind="unresolved", restoration_status="unknown"), "现实结果仍未知"),\n    ]\n    for row, expected in cases:\n        projection = project_repair(row)\n        assert projection["semantic_summary"] == expected\n        assert projection["integrity_violations"] == []\n\n\ndef test_integrity_conflict_never_renders_positive_claim() -> None:\n    projection = project_repair(_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs="[]", bases='["basis:restored"]'))\n    assert projection["semantic_summary"] == "语义完整性异常，需检查"\n    assert projection["integrity_violations"] == ["restored_without_proof"]\n\n\ndef test_malformed_lineage_is_not_evidence_and_legacy_result_is_not_semantics() -> None:\n    projection = project_repair(_row(status="closed", resolution_kind="restored", restoration_status="verified", proofs="not-json", bases='["basis:restored"]', result="rejected"))\n    assert projection["restoration_proof_refs"] == []\n    assert projection["semantic_summary"] == "语义完整性异常，需检查"\n    first = project_repair(_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]', result="verified"))\n    second = project_repair(_row(status="closed", resolution_kind="rejected", restoration_status="unverified", bases='["basis:reject"]', result="different-legacy-text"))\n    assert first == second\n\n\ndef _config(tmp_path) -> ControlPlaneConfig:\n    return replace(ControlPlaneConfig(), api_key="secret", data_dir=tmp_path / "data", patch_dir=tmp_path / "data" / "patches", evidence_dir=tmp_path / "data" / "evidence", state_db=tmp_path / "data" / "control-plane.db", notification_enabled=False, model_preflight_enabled=False)\n\n\ndef test_status_and_evidence_expose_same_additive_projection(tmp_path) -> None:\n    app = create_app(_config(tmp_path))\n    store: Store = app.state.store\n    store.create_repair("repair-c4b-api", "fp-c4b", "{}")\n    store.set_repair_status("repair-c4b-api", "closed", result="legacy-result", finished_at=1)\n    store.set_repair_resolution("repair-c4b-api", resolution_kind="rejected", restoration_status="unverified", basis_refs=["basis:reject"])\n    client = TestClient(app)\n    headers = {"X-Control-Plane-Key": "secret"}\n    status_row = next(row for row in client.get("/status", headers=headers).json()["recent_repairs"] if row["id"] == "repair-c4b-api")\n    evidence_row = next(row for row in client.get("/v1/evidence", headers=headers).json()["repairs"] if row["id"] == "repair-c4b-api")\n    semantic_keys = {"status", "resolution_kind", "restoration_status", "semantic_summary", "restoration_proof_refs", "resolution_basis_refs", "integrity_violations"}\n    assert {key: status_row[key] for key in semantic_keys} == {key: evidence_row[key] for key in semantic_keys}\n    assert status_row["result"] == "legacy-result"\n    assert evidence_row["result"] == "legacy-result"\n\n\ndef test_metrics_snapshot_and_fixed_integrity_counts(tmp_path) -> None:\n    store = Store(tmp_path / "cp.db")\n    for repair_id in ("r-closed-unverified", "r-restored-no-proof", "r-no-basis"):\n        store.create_repair(repair_id, repair_id, "{}")\n    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='restored', restoration_status='unverified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[\\\"basis:1\\\"]' WHERE id='r-closed-unverified'")  # noqa: SLF001\n    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='restored', restoration_status='verified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[\\\"basis:2\\\"]' WHERE id='r-restored-no-proof'")  # noqa: SLF001\n    store._connection.execute("UPDATE repairs SET status='closed', resolution_kind='rejected', restoration_status='unverified', restoration_proof_refs_json='[]', resolution_basis_refs_json='[]' WHERE id='r-no-basis'")  # noqa: SLF001\n    families = {family.name: family for family in ControlPlaneCollector(store, lambda: 5).collect()}\n    resolution = {sample.labels["resolution_kind"]: sample.value for sample in families["control_plane_repairs_by_resolution"].samples}\n    restoration = {sample.labels["restoration_status"]: sample.value for sample in families["control_plane_repairs_by_restoration"].samples}\n    integrity = {sample.labels["kind"]: sample.value for sample in families["control_plane_semantic_integrity_violations"].samples}\n    assert resolution == {"rejected": 1, "restored": 2}\n    assert restoration == {"unverified": 2, "verified": 1}\n    assert integrity == {"closed_restored_unverified": 1, "restored_without_proof": 2, "non_unresolved_without_basis": 1}\n    assert set(integrity) == set(INTEGRITY_KINDS)\n    store.close()\n\n\ndef test_terminal_notifications_use_fresh_post_authority_projection() -> None:\n    source = Path("src/control_plane/service.py").read_text(encoding="utf-8-sig")\n    assert "def _fresh_outward_projection" in source\n    assert "row = self.store.get_repair(repair_id)" in source\n    assert "return project_repair(row)" in source\n    for authority_call in ("self.closure_authority.close_rejected(repair_id)", "self.closure_authority.record_rolled_back(repair_id, receipts)", "self.closure_authority.close_restored(repair_id)"):\n        index = source.index(authority_call)\n        assert index < source.index("_fresh_outward_projection(repair_id)", index)\n    assert 'await self._notify("info", "修复已被拒绝"' not in source\n    assert 'await self._notify("warning", "修复已回滚"' not in source\n    assert 'await self._notify("info", "验证通过"' not in source\n    assert 'f"修复完成：' not in source\n\n\ndef test_outward_layer_has_no_authority_or_mutation_seam() -> None:\n    source = Path("src/control_plane/outward_semantics.py").read_text(encoding="utf-8-sig")\n    assert "ClosureAuthority" not in source\n    assert "from .storage import Store" not in source\n    assert "set_repair_" not in source\n    assert "UPDATE repairs" not in source\n    assert "INSERT INTO" not in source\n\n\ndef test_integrity_flags_are_fixed_and_observational() -> None:\n    assert integrity_violation_flags(_row(status="closed", resolution_kind="restored", restoration_status="unverified", proofs="[]", bases="[]")) == {"closed_restored_unverified": True, "restored_without_proof": True, "non_unresolved_without_basis": True}\n''',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
