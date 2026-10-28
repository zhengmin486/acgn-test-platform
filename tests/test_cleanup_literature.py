"""scripts/cleanup_literature.py 的行为测试：默认只统计、--apply 才删。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import cleanup_literature  # noqa: E402

SCHEMA = """
CREATE TABLE contents (
    id INTEGER PRIMARY KEY, title TEXT, type TEXT, book_kind TEXT,
    rating REAL, bangumi_id INTEGER, image_url TEXT, remote_image_url TEXT,
    year INTEGER, series_id INTEGER
);
CREATE TABLE content_favorites (id INTEGER PRIMARY KEY, user_id INTEGER, content_id INTEGER);
CREATE TABLE content_view_history (id INTEGER PRIMARY KEY, user_id INTEGER, content_id INTEGER);
CREATE TABLE content_tags (id INTEGER PRIMARY KEY, content_id INTEGER, tag_id INTEGER);
CREATE TABLE content_submissions (
    id INTEGER PRIMARY KEY, user_id INTEGER, content_id INTEGER,
    applied_content_id INTEGER, title TEXT
);
CREATE TABLE content_relations (
    id INTEGER PRIMARY KEY, from_content_id INTEGER, to_content_id INTEGER,
    relation_type TEXT
);
CREATE TABLE content_credits (id INTEGER PRIMARY KEY, content_id INTEGER);
CREATE TABLE content_cast (id INTEGER PRIMARY KEY, content_id INTEGER);
CREATE TABLE content_facts (id INTEGER PRIMARY KEY, content_id INTEGER);
CREATE TABLE content_studios (id INTEGER PRIMARY KEY, content_id INTEGER);
CREATE TABLE article_content_links (id INTEGER PRIMARY KEY, content_id INTEGER);
CREATE TABLE content_entity_links (id INTEGER PRIMARY KEY, content_id INTEGER);
"""


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO contents (id, title, type, book_kind, rating, bangumi_id) VALUES (?,?,?,?,?,?)",
        [
            (1, "文学A", "novel", "literature", 7.0, 101),
            (2, "轻小说B", "novel", "light_novel", 8.0, 102),
            (3, "文学C", "novel", "literature", 6.0, 103),
            (4, "动画D", "anime", None, 9.0, 104),
        ],
    )
    conn.executemany(
        "INSERT INTO content_favorites (user_id, content_id) VALUES (?,?)",
        [(1, 1), (1, 2), (2, 1), (2, 3)],
    )
    conn.executemany(
        "INSERT INTO content_view_history (user_id, content_id) VALUES (?,?)",
        [(1, 1), (3, 4)],
    )
    conn.execute("INSERT INTO content_tags (content_id, tag_id) VALUES (1, 10)")
    conn.executemany(
        "INSERT INTO content_submissions (user_id, content_id, applied_content_id, title) VALUES (?,?,?,?)",
        [(1, 1, None, "改文学A"), (2, None, 3, "新增指向文学C")],
    )
    conn.executemany(
        "INSERT INTO content_relations (from_content_id, to_content_id, relation_type) VALUES (?,?,?)",
        [(1, 4, "关联"), (2, 3, "关联")],
    )
    conn.commit()
    conn.close()


def _query(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_stats_mode_deletes_nothing(tmp_path: Path, capsys):
    db = tmp_path / "t.db"
    _seed(db)
    rc = cleanup_literature.main(["--db", str(db)])
    assert rc == 0
    # 数据原样
    assert _query(db, "SELECT COUNT(*) FROM contents")[0][0] == 4
    assert _query(db, "SELECT COUNT(*) FROM content_favorites")[0][0] == 4
    out = capsys.readouterr().out
    assert "待清理文学作品" in out and "2 条" in out
    assert "content_favorites.content_id: 3 行" in out
    assert "2 名用户收藏过" in out  # user1 收藏了文学1，user2 收藏了文学1/3


def test_stats_mode_no_literature(tmp_path: Path, capsys):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO contents (id,title,type,book_kind,rating) VALUES (1,'轻','novel','light_novel',8)")
    conn.commit()
    conn.close()
    assert cleanup_literature.main(["--db", str(db)]) == 0
    assert "无需清理" in capsys.readouterr().out


def test_apply_removes_literature_and_dependents(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed(db)
    backup = tmp_path / "backup.json"
    rc = cleanup_literature.main(["--db", str(db), "--apply", "--backup", str(backup)])
    assert rc == 0

    # contents：只剩轻小说与动画
    rows = _query(db, "SELECT id, type, book_kind FROM contents ORDER BY id")
    assert rows == [(2, "novel", "light_novel"), (4, "anime", None)]
    # 收藏/浏览/标签：文学相关行没了，非文学行保留
    assert _query(db, "SELECT content_id FROM content_favorites ORDER BY content_id") == [(2,)]
    assert _query(db, "SELECT content_id FROM content_view_history") == [(4,)]
    assert _query(db, "SELECT COUNT(*) FROM content_tags")[0][0] == 0
    # submissions：不删，仅置 NULL
    subs = _query(db, "SELECT id, content_id, applied_content_id FROM content_submissions ORDER BY id")
    assert subs == [(1, None, None), (2, None, None)]
    # content_relations：两侧引用都清（1->4 与 2->3 都涉及文学 1/3）
    assert _query(db, "SELECT COUNT(*) FROM content_relations")[0][0] == 0

    # 备份可回灌：2 条 literature contents + 依赖快照
    data = json.loads(backup.read_text(encoding="utf-8"))
    assert {c["id"] for c in data["contents"]} == {1, 3}
    assert "content_favorites.content_id" in data["dependents"]
    assert len(data["dependents"]["content_favorites.content_id"]) == 3


def test_apply_requires_backup(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed(db)
    with pytest.raises(SystemExit):
        cleanup_literature.main(["--db", str(db), "--apply"])
