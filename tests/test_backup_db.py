"""备份脚本测试：scripts/backup_db.py 的在线备份、自检与保留策略。

覆盖：
- URL 解析：相对 / 绝对 SQLite 路径、非 SQLite 报错、空路径报错；
- 备份产物：时间戳文件名、与源库一致的数据、integrity_check 自检；
- 保留策略：只清理 acgn-*.db、不碰其它文件、retain=0 不清理、
  刚生成的备份永远保留；
- 失败路径：源库损坏 / 文件不存在 / 非 SQLite 连接串，均非 0 退出且
  不残留坏备份文件。

使用独立临时 SQLite（tmp_path），与开发库 local_dev.db 完全隔离。
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import build_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backup_db.py"

# scripts/ 不是包，按文件路径加载被测 CLI 模块（与直接运行脚本等价）。
_spec = importlib.util.spec_from_file_location("backup_db_for_tests", SCRIPT_PATH)
backup_db = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = backup_db
_spec.loader.exec_module(backup_db)


def _make_source_db(path: Path) -> str:
    """建一个带种子数据的 SQLite 库，返回连接串。"""
    url = f"sqlite:///{path}"
    engine = build_engine(url)
    crud.init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        crud.create_user(session, username="backup_probe", password="probe-pass-123",
                         role="user")
    finally:
        session.close()
    engine.dispose()
    return url


def _read_usernames(db_file: Path) -> list[str]:
    """直接只读打开备份文件，返回 users 表全部用户名（证明快照可用）。"""
    conn = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT username FROM users ORDER BY id").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


# ---------- URL 解析 ----------


class TestSqlitePathFromUrl:
    def test_relative_path_resolves_against_project_root(self):
        path = backup_db.sqlite_path_from_url("sqlite:///./local_dev.db")
        assert path == (PROJECT_ROOT / "local_dev.db").resolve()

    def test_absolute_path(self, tmp_path):
        db = tmp_path / "abs.db"
        path = backup_db.sqlite_path_from_url(f"sqlite:///{db}")
        assert path == db.resolve()

    def test_rejects_non_sqlite(self):
        for url in (
            "mysql+pymysql://acgn_app:pw@127.0.0.1:3306/acgn?charset=utf8mb4",
            "postgresql://user:pw@localhost/acgn",
        ):
            try:
                backup_db.sqlite_path_from_url(url)
            except ValueError as exc:
                assert "SQLite" in str(exc)
            else:  # pragma: no cover - 失败路径
                raise AssertionError(f"应当拒绝非 SQLite 连接串：{url}")

    def test_rejects_empty_path(self):
        try:
            backup_db.sqlite_path_from_url("sqlite:///")
        except ValueError as exc:
            assert "数据库文件路径" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当拒绝空数据库路径")


# ---------- 备份主流程 ----------


class TestBackup:
    def test_creates_timestamped_file_with_same_data(self, tmp_path, capsys):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        out_dir = tmp_path / "out"

        code = backup_db.main(["--database-url", url, "--output-dir", str(out_dir)])
        assert code == 0

        backups = list(out_dir.glob("acgn-*.db"))
        assert len(backups) == 1
        usernames = _read_usernames(backups[0])
        # init_db 建的默认账号 + 我们探针账号，全部在快照里
        assert usernames == ["admin", "demo", "backup_probe"]

        printed = capsys.readouterr().out
        assert "完整性自检 ok" in printed

    def test_custom_output_path(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        target = tmp_path / "nested" / "before-upgrade.db"

        code = backup_db.main(["--database-url", url, "--output", str(target)])
        assert code == 0
        assert target.is_file()
        assert "backup_probe" in _read_usernames(target)

    def test_list_only(self, tmp_path, capsys):
        out_dir = tmp_path / "empty-out"
        out_dir.mkdir()
        assert backup_db.main(["--list", "--output-dir", str(out_dir)]) == 0
        assert "还没有备份" in capsys.readouterr().out

    def test_missing_db_file(self, tmp_path, capsys):
        url = f"sqlite:///{tmp_path / 'not-there.db'}"
        code = backup_db.main(["--database-url", url,
                               "--output-dir", str(tmp_path / "out")])
        assert code == 1
        assert "不存在" in capsys.readouterr().out
        assert not list((tmp_path / "out").glob("acgn-*.db"))

    def test_rejects_non_sqlite_url(self, tmp_path, capsys):
        code = backup_db.main([
            "--database-url", "mysql+pymysql://u:p@127.0.0.1:3306/acgn",
            "--output-dir", str(tmp_path / "out"),
        ])
        assert code == 1
        assert "只支持 SQLite" in capsys.readouterr().out

    def test_corrupt_source_leaves_no_backup(self, tmp_path, capsys):
        source = tmp_path / "broken.db"
        source.write_bytes(b"this is definitely not a sqlite database")
        out_dir = tmp_path / "out"

        code = backup_db.main([
            "--database-url", f"sqlite:///{source}",
            "--output-dir", str(out_dir),
        ])
        assert code == 1
        assert "备份失败" in capsys.readouterr().out
        assert list(out_dir.glob("acgn-*.db")) == []


# ---------- 保留策略 ----------


def _make_fake_backup(out_dir: Path, name: str, age_days: float) -> Path:
    """在输出目录造一个假的旧备份（只有文件名符合规则，内容随意）。"""
    path = out_dir / name
    path.write_bytes(b"fake")
    old = os.stat(path).st_mtime - age_days * 86400
    os.utime(path, (old, old))
    return path


class TestPrune:
    def test_keeps_newest_and_removes_older(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        for day in range(5, 0, -1):
            _make_fake_backup(out_dir, f"acgn-2026010{day}-120000.db", age_days=day)

        code = backup_db.main(["--database-url", url, "--output-dir", str(out_dir),
                               "--retain", "2"])
        assert code == 0

        remaining = sorted(p.name for p in out_dir.glob("acgn-*.db"))
        # 只剩今天新生成的 + 最旧那个假备份之外……精确说：新备份(最新) + 1 天前那个
        assert len(remaining) == 2
        assert any(name.startswith("acgn-") and "120000" not in name
                   for name in remaining), remaining
        assert "acgn-20260101-120000.db" in remaining
        assert "acgn-20260105-120000.db" not in remaining

    def test_retain_zero_disables_cleanup(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _make_fake_backup(out_dir, "acgn-20200101-000000.db", age_days=1000)

        code = backup_db.main(["--database-url", url, "--output-dir", str(out_dir),
                               "--retain", "0"])
        assert code == 0
        assert len(list(out_dir.glob("acgn-*.db"))) == 2

    def test_other_files_are_untouched(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "notes.txt").write_text("keep me", encoding="utf-8")
        (out_dir / "local_dev.db").write_bytes(b"keep me too")
        _make_fake_backup(out_dir, "acgn-20200101-000000.db", age_days=1000)

        code = backup_db.main(["--database-url", url, "--output-dir", str(out_dir),
                               "--retain", "1"])
        assert code == 0
        assert (out_dir / "notes.txt").is_file()
        assert (out_dir / "local_dev.db").is_file()
        assert len(list(out_dir.glob("acgn-*.db"))) == 1


# ---------- 纯函数 ----------


class TestHelpers:
    def test_human_size(self):
        assert backup_db.human_size(512) == "0.5 KB"
        assert backup_db.human_size(3 * 1024 * 1024) == "3.0 MB"

    def test_online_backup_preserves_rows(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        target = tmp_path / "copy.db"
        backup_db.online_backup(source, target)
        assert "backup_probe" in _read_usernames(target)

    def test_verify_backup_detects_corruption(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"garbage")
        verdict = backup_db.verify_backup(broken)
        assert verdict != "ok"

    def test_verify_backup_ok_for_real_db(self, tmp_path):
        source = tmp_path / "src.db"
        url = _make_source_db(source)
        assert backup_db.verify_backup(source) == "ok"
