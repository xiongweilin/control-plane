"""control-plane 状态库的独立备份与恢复演练。

为什么需要它：`data/` 下的状态库保存的是本平台自己做过什么——kernel 运行记录、
repair/alert 历史、command audit、settings。它不是可重建的缓存，所以结论不是
ephemeral，而是"必须有独立于 data/ 的快照，并且定期真的恢复一次并验证语义"。

用法：
    uv run python scripts/backup-state.py backup [--state-dir DIR] [--backup-root DIR]
    uv run python scripts/backup-state.py restore-drill --snapshot DIR
    uv run python scripts/backup-state.py verify --snapshot DIR   # 只校验完整性，不建库

设计要点：
  - 快照用 SQLite 在线备份 API（`Connection.backup`），对运行中的 WAL 库安全；
  - 每个文件记录 sha256 与表行数，manifest.json 是该快照的唯一说明；
  - 恢复演练把快照解到隔离目录，只读打开，核对 integrity_check、表集合与行数，
    再做一次语义抽样读取（runtime_records / settings / alerts），任一失败即非零退出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = PROJECT_ROOT / "data"
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups" / "state"
SEMANTIC_SAMPLES = {
    "agent-kernel.db": ("runtime_records",),
    "control-plane.db": ("settings", "alerts", "command_audit"),
}


def _state_databases(state_dir: Path) -> list[Path]:
    return sorted(path for path in state_dir.glob("*.db") if path.is_file())


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in connection.execute(
            "select name from sqlite_master where type = ? order by name", ("table",)
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = connection.execute(f'select count(*) from "{table}"').fetchone()[0]
    return counts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(connection.execute("pragma integrity_check").fetchone()[0])
    finally:
        connection.close()


def backup(state_dir: Path, backup_root: Path) -> int:
    sources = _state_databases(state_dir)
    if not sources:
        print(f"no state databases under {state_dir}")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_root / stamp
    target.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, object] = {
        "created": stamp,
        "source_dir": str(state_dir),
        "files": [],
    }
    files: list[dict[str, object]] = []
    for source in sources:
        destination = target / source.name
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()

        check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            counts = _table_counts(check)
            integrity = str(check.execute("pragma integrity_check").fetchone()[0])
        finally:
            check.close()

        files.append(
            {
                "name": source.name,
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
                "integrity_check": integrity,
                "rows": counts,
            }
        )
        print(
            f"  snapshot {source.name}: {destination.stat().st_size} bytes, integrity={integrity}"
        )

    manifest["files"] = files
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"backup written to {target}")
    return 0 if all(item["integrity_check"] == "ok" for item in files) else 1


def restore_drill(snapshot: Path, *, restore: bool) -> int:
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        print(f"missing manifest: {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    problems: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="control-plane-restore-drill-")) if restore else snapshot
    try:
        for entry in manifest["files"]:
            name = entry["name"]
            source = snapshot / name
            if restore:
                restored = workdir / name
                shutil.copyfile(source, restored)
            else:
                restored = source

            if _sha256(source) != entry["sha256"]:
                problems.append(f"{name}: sha256 does not match the manifest")
            integrity = _integrity(restored)
            if integrity != "ok":
                problems.append(f"{name}: integrity_check={integrity}")

            connection = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
            try:
                counts = _table_counts(connection)
                for table, expected in entry["rows"].items():
                    actual = counts.get(table)
                    if actual != expected:
                        problems.append(f"{name}.{table}: {actual} rows, manifest says {expected}")
                for table in SEMANTIC_SAMPLES.get(name, ()):
                    if table not in counts:
                        problems.append(
                            f"{name}: expected table {table!r} is missing after restore"
                        )
                        continue
                    sample = connection.execute(f'select * from "{table}" limit 1').fetchone()
                    if counts[table] and sample is None:
                        problems.append(f"{name}.{table}: semantic sample read returned nothing")
            finally:
                connection.close()
            print(f"  restored {name}: rows match manifest, integrity ok")
    finally:
        if restore:
            shutil.rmtree(workdir, ignore_errors=True)

    if problems:
        print("restore drill failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"restore drill passed for snapshot {snapshot.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser("backup", help="create a snapshot of the state databases")
    backup_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    backup_parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))

    drill_parser = sub.add_parser(
        "restore-drill", help="restore a snapshot in isolation and verify"
    )
    drill_parser.add_argument("--snapshot", required=True)

    verify_parser = sub.add_parser("verify", help="verify a snapshot in place")
    verify_parser.add_argument("--snapshot", required=True)

    args = parser.parse_args()
    if args.command == "backup":
        return backup(Path(args.state_dir), Path(args.backup_root))
    if args.command == "restore-drill":
        return restore_drill(Path(args.snapshot), restore=True)
    return restore_drill(Path(args.snapshot), restore=False)


if __name__ == "__main__":
    raise SystemExit(main())
