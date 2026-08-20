"""Store read switching and State Export bundle (S19-20, S31-32, S52-53) tests."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from control_plane.storage import Store
from portable_runtime.api.cli import run_cli
from portable_runtime.core.models import Artifact, Evidence, KnowledgeItem, Work
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.bundle import bundle_contains_absolute_paths, export_bundle, import_bundle
from portable_runtime.stores.filesystem import FilesystemArtifactStore
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.stores.sqlite import SQLiteStateStore


def test_storage_portable_read_switch(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy.db"
    store = Store(legacy_db)
    store.create_repair("repair-1", "fp-1", '{"alert":1}')
    portable_db = tmp_path / "portable.db"
    portable = SQLiteStateStore(portable_db)
    from portable_runtime.compat.legacy_control_plane import import_legacy_repair

    work, run = import_legacy_repair(
        {"id": "repair-1", "fingerprint": "fp-1", "status": "closed", "payload_json": '{"alert":1}'},
        portable,
    )
    assert run is not None
    store.attach_portable_store(portable, enable_read=True)
    via = store.get_repair_via_portable("repair-1")
    assert via is not None
    assert via["id"] == "repair-1"
    assert via["source"] == "portable"
    assert via["portable_work_id"] == work.id
    fallback = store.get_repair_with_fallback("repair-1")
    assert isinstance(fallback, dict)
    assert fallback["source"] == "portable"
    store.detach_portable_store()
    fallback2 = store.get_repair_with_fallback("repair-1")
    assert fallback2 is not None
    assert fallback2["id"] == "repair-1" if isinstance(fallback2, dict) else fallback2["id"] == "repair-1"
    store.attach_portable_store(portable, enable_read=True)
    lst = store.list_repairs_via_portable(limit=10)
    assert any(item["id"] == "repair-1" for item in lst)
    lst_fallback = store.list_repairs_with_fallback(limit=10)
    assert any(item["id"] == "repair-1" for item in lst_fallback)
    ki = KnowledgeItem(id="k1", kind="procedure", title="t", content_ref="c", status="candidate")
    portable.save_knowledge(ki)
    cands = store.list_candidates_via_portable(status="candidate")
    assert any(getattr(c, "id", "") == "k1" for c in cands)
    fetched = store.get_knowledge_for_candidate("k1")
    assert fetched is not None
    store.close()
    portable.close()


def test_storage_portable_attach_without_read_enabled(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy2.db"
    store = Store(legacy_db)
    store.create_repair("repair-2", "fp-2", "{}")
    portable = InMemoryStateStore()
    from portable_runtime.compat.legacy_control_plane import import_legacy_repair

    import_legacy_repair({"id": "repair-2", "fingerprint": "fp-2", "status": "open", "payload_json": "{}"}, portable)
    store.attach_portable_store(portable, enable_read=False)
    result = store.get_repair_with_fallback("repair-2")
    assert result is not None
    if isinstance(result, dict):
        assert result.get("source") != "portable" or result["source"] != "portable"
    store.close()


def test_bundle_manifest_and_no_absolute_paths(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    artifact_root = tmp_path / "artifacts"
    state = SQLiteStateStore(state_db)
    artifacts = FilesystemArtifactStore(artifact_root)
    runtime = Runtime(store=state, artifact_store=artifacts, runtime_id="test-runtime")
    w1 = runtime.create_work(title="work-one", kind="incident", description="payload1")
    _w2 = runtime.create_work(title="work-two", kind="generic-task")
    blob = b"hello artifact for bundle"
    uri = artifacts.put(blob, media_type="text/plain")
    art = Artifact(id="art1", kind="report", media_type="text/plain", uri=uri, checksum="sha256")
    state.save_artifact(art)
    ev = Evidence(id="ev1", kind="test", subject_refs=[w1.id], source="unit", status="supported")
    state.save_evidence(ev)
    bundle_path = tmp_path / "runtime-state.tar.zst"
    export_bundle(state, artifacts, bundle_path, runtime_id=runtime.runtime_id)
    assert bundle_path.exists()
    assert not bundle_contains_absolute_paths(bundle_path)
    raw = bundle_path.read_bytes()
    import gzip
    tar_bytes = raw
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        import zstandard as zstd  # type: ignore
        tar_bytes = zstd.ZstdDecompressor().decompress(raw, max_output_size=50 * 1024 * 1024)
    elif raw[:2] == b"\x1f\x8b":
        tar_bytes = gzip.decompress(raw)
    import io
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        names = [m.name for m in tar.getmembers()]
        assert "manifest.json" in names
        assert "works.jsonl" in names
        assert "runs.jsonl" in names
        assert "evidence.jsonl" in names
        assert "artifacts.jsonl" in names
        assert any(n.startswith("artifacts/") for n in names)
        for n in names:
            assert not n.startswith("/")
            assert not n.startswith("\\")
            assert ".." not in Path(n).parts
            assert "D:" not in n and "C:" not in n
        mf = tar.extractfile(tar.getmember("manifest.json"))
        assert mf is not None
        manifest = json.loads(mf.read().decode("utf-8"))
        assert manifest["schema_version"] == "1"
        assert manifest["runtime_id"] == "test-runtime"
        assert manifest["counts"]["work"] >= 2
        for member in tar.getmembers():
            if member.name.endswith(".jsonl"):
                f = tar.extractfile(member)
                assert f is not None
                text = f.read().decode("utf-8")
                assert "D:\\" not in text
                assert "C:\\" not in text
    state.close()


def test_sqlite_filesystem_bundle_roundtrip_preserves_ids(tmp_path: Path) -> None:
    src_db = tmp_path / "src.db"
    src_art_root = tmp_path / "src_art"
    src_state = SQLiteStateStore(src_db)
    src_art = FilesystemArtifactStore(src_art_root)
    src_runtime = Runtime(store=src_state, artifact_store=src_art)
    w = src_runtime.create_work(title="preserve", kind="research", description="keep-id")
    r = src_runtime.start_run(w.id, workflow_id="generic-task")
    data = b"cross-platform artifact bytes - should survive Windows->Linux"
    uri = src_art.put(data)
    art = Artifact(id="art-preserve", kind="dataset", uri=uri, created_by_run_id=r.id)
    src_state.save_artifact(art)
    from portable_runtime.core.models import KnowledgeItem
    ki = KnowledgeItem(id="know1", kind="procedure", title="proc", content_ref="ref", status="candidate")
    src_state.save_knowledge(ki)
    bundle = tmp_path / "bundle.tar.zst"
    src_state.export_bundle(bundle, artifact_store=src_art, runtime_id="src")
    tgt_db = tmp_path / "tgt.db"
    tgt_art_root = tmp_path / "tgt_art"
    tgt_state = SQLiteStateStore(tgt_db)
    tgt_art = FilesystemArtifactStore(tgt_art_root)
    manifest = tgt_state.import_bundle(bundle, artifact_store=tgt_art)
    assert manifest["schema_version"] == "1"
    restored_w = tgt_state.get_work(w.id)
    assert restored_w is not None
    assert restored_w.id == w.id
    assert restored_w.title == w.title
    restored_r = tgt_state.get_run(r.id)
    assert restored_r is not None
    assert restored_r.id == r.id
    restored_art = tgt_state.get_artifact("art-preserve")
    assert restored_art is not None
    restored_bytes = tgt_art.get(restored_art.uri)  # type: ignore[arg-type]
    assert restored_bytes == data
    assert tgt_state.get_knowledge("know1") is not None
    src_state.close()
    tgt_state.close()


def test_filesystem_helpers_and_scoping(tmp_path: Path) -> None:
    root = tmp_path / "fs_root"
    store = FilesystemArtifactStore(root)
    store.put(b"alpha")
    store.put(b"beta")
    digests = store.export_artifacts()
    assert len(digests) == 2
    assert all(isinstance(d, str) and len(d) == 64 for d in digests)
    uris = store.list_artifact_uris()
    assert len(uris) == 2
    import hashlib
    data = b"imported bytes"
    digest = hashlib.sha256(data).hexdigest()
    p = store.import_artifact_bytes(digest, data)
    assert p.exists()
    assert p.read_bytes() == data
    with pytest.raises(ValueError):
        store.import_artifact_bytes("../escape", b"x")
    with pytest.raises(ValueError):
        store.import_artifact_bytes("a/b", b"x")
    with pytest.raises(ValueError):
        store.get((tmp_path / "outside").as_uri())


def test_cli_bundle_export_import_roundtrip(tmp_path: Path) -> None:
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    art_a_root = tmp_path / "art_a"
    state_a = SQLiteStateStore(db_a)
    art_a = FilesystemArtifactStore(art_a_root)
    rt_a = Runtime(store=state_a, artifact_store=art_a)
    rt_a.create_work(title="cli-work", kind="incident")
    blob = b"cli-artifact"
    uri = art_a.put(blob)
    rt_a.store.save_artifact(Artifact(id="art-cli", kind="report", uri=uri))
    bundle_path = tmp_path / "cli-bundle.tar.zst"
    rt_a.export_bundle(bundle_path)
    assert bundle_path.exists()
    assert not bundle_contains_absolute_paths(bundle_path)
    rc = run_cli(["--state", str(db_b), "state", "import", str(bundle_path)])
    assert rc == 0
    state_b = SQLiteStateStore(db_b)
    works = state_b.list_work()
    assert any(w.title == "cli-work" for w in works)
    state_a.close()
    state_b.close()


def test_bundle_rejects_absolute_and_traversal(tmp_path: Path) -> None:
    import gzip
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="/tmp/evil.txt")  # noqa: S108
        data = b"evil"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    gz_path = tmp_path / "malicious.tar.zst"
    gz_path.write_bytes(gzip.compress(raw))
    state = InMemoryStateStore()
    with pytest.raises(ValueError, match="unsafe"):
        import_bundle(state, None, gz_path)
    buf2 = io.BytesIO()
    with tarfile.open(fileobj=buf2, mode="w") as tar:
        info = tarfile.TarInfo(name="artifacts/../../escape.txt")
        data = b"evil2"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw2 = buf2.getvalue()
    gz_path2 = tmp_path / "mal2.tar.zst"
    gz_path2.write_bytes(gzip.compress(raw2))
    with pytest.raises(ValueError):
        import_bundle(state, None, gz_path2)


def test_inmemory_bundle_memory_store(tmp_path: Path) -> None:
    mem = InMemoryStateStore()
    w = Work(id="w1", title="mem-work", kind="generic-task")
    mem.save_work(w)
    bundle = tmp_path / "mem.tar.zst"
    export_bundle(mem, None, bundle, runtime_id="mem-test")
    assert bundle.exists()
    assert not bundle_contains_absolute_paths(bundle)
    mem2 = InMemoryStateStore()
    import_bundle(mem2, None, bundle)
    assert mem2.get_work("w1") is not None
    assert mem2.get_work("w1").title == "mem-work"  # type: ignore[union-attr]
