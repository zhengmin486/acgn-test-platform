"""清理生产库里已上线的文学作品（book_kind=literature 的 novel）。

背景：v3 收录口径（2026-10-23）明确「轻小说只放轻小说」，线上历史数据里
混有 book_kind=literature 的日本普通文学，需要清掉，与轻小说区只保留
文库轻小说的口径对齐。

安全设计：
- **默认只统计**（stats 模式），打印待删条数、各依赖表关联行数、抽样清单；
  不碰任何数据。确认无误后再加 ``--apply`` 执行删除。
- ``--apply`` 采用单事务；删除前先把**完整的目标行快照**（contents 行 +
  各依赖表行）写成 JSON 备份文件（``--backup`` 指定路径），可人工回灌。
- content_submissions 的 content_id / applied_content_id 只置 NULL
  （保留用户的申请与审核痕迹），不删申请本身。
- content_relations 是双向引用（from_content_id / to_content_id），两侧都清。
-  busy_timeout=60，避免与 uvicorn / 定时任务共写时 database is locked。

用法（服务器侧，cwd 必须是 /opt/acgn-test-platform，见 memory/ingest.md 相对路径陷阱）：
    # 1) 先看影响面
    .venv/bin/python3 scripts/cleanup_literature.py --db /opt/acgn-test-platform/local_dev.db
    # 2) 确认后执行（自动先写备份）
    .venv/bin/python3 scripts/cleanup_literature.py --db /opt/acgn-test-platform/local_dev.db \\
        --apply --backup /opt/acgn-test-platform/backups/literature_cleanup_$(date +%Y%m%d).json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

# (表名, 引用列) —— content_relations 两侧引用分开列
DEPENDENT_TABLES: list[tuple[str, str]] = [
    ("content_credits", "content_id"),
    ("content_cast", "content_id"),
    ("content_facts", "content_id"),
    ("content_studios", "content_id"),
    ("content_favorites", "content_id"),
    ("content_view_history", "content_id"),
    ("content_submissions", "content_id"),
    ("content_tags", "content_id"),
    ("article_content_links", "content_id"),
    ("content_entity_links", "content_id"),
    ("content_relations", "from_content_id"),
    ("content_relations", "to_content_id"),
]

CONTENT_COLUMNS = [
    "id", "title", "type", "book_kind", "rating", "bangumi_id",
    "image_url", "remote_image_url", "year", "series_id",
]


def collect_target_ids(conn) -> list[int]:
    rows = conn.execute(
        text("SELECT id FROM contents WHERE type='novel' AND book_kind='literature' ORDER BY id")
    ).fetchall()
    return [int(r[0]) for r in rows]


def stats_mode(conn, ids: list[int]) -> int:
    if not ids:
        print("没有 book_kind=literature 的 novel，无需清理。")
        return 0

    total = len(ids)
    print(f"待清理文学作品（novel + book_kind=literature）：{total} 条")

    # 依赖表关联行数（按表汇总，一次 IN 查询）
    id_list = ",".join(str(i) for i in ids)
    dep_total = 0
    for table, column in DEPENDENT_TABLES:
        n = conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({id_list})")
        ).scalar_one()
        if n:
            print(f"  依赖 {table}.{column}: {n} 行")
            dep_total += int(n)
    print(f"依赖行合计：{dep_total}")

    # 抽样看 15 条，人工核对是不是真文学
    print("\n抽样（前 15 条）：")
    sample = conn.execute(
        text(
            "SELECT id, title, rating, bangumi_id FROM contents "
            "WHERE type='novel' AND book_kind='literature' ORDER BY id LIMIT 15"
        )
    ).fetchall()
    for row in sample:
        print(f"  #{row[0]} rating={row[2]} bgm={row[3]} {row[1]}")

    # 收藏 / 浏览记录涉及的用户侧影响（这两种是用户数据，删除即丢失用户痕迹）
    fav = conn.execute(
        text(
            "SELECT COUNT(DISTINCT user_id) FROM content_favorites "
            f"WHERE content_id IN ({id_list})"
        )
    ).scalar_one()
    his = conn.execute(
        text(
            "SELECT COUNT(DISTINCT user_id) FROM content_view_history "
            f"WHERE content_id IN ({id_list})"
        )
    ).scalar_one()
    print(f"\n用户影响：{fav} 名用户收藏过这些作品；{his} 名用户有浏览记录（--apply 会一并删除）")
    print("\n以上是影响面统计（未改动任何数据）。确认后加 --apply 执行。")
    return 0


def apply_mode(conn, ids: list[int], backup_path: Path) -> int:
    if not ids:
        print("没有可清理的条目。")
        return 0

    backup_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) 备份：contents 全列 + 各依赖表现状
    id_list = ",".join(str(i) for i in ids)
    col_sql = ", ".join(CONTENT_COLUMNS)
    content_rows = [
        dict(zip(CONTENT_COLUMNS, row))
        for row in conn.execute(
            text(f"SELECT {col_sql} FROM contents WHERE id IN ({id_list})")
        ).fetchall()
    ]
    backup: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": "cleanup book_kind=literature novels (ingest policy v3, 2026-10-23)",
        "content_ids": ids,
        "contents": content_rows,
        "dependents": {},
    }
    for table, column in DEPENDENT_TABLES:
        rows = conn.execute(
            text(f"SELECT * FROM {table} WHERE {column} IN ({id_list})")
        ).fetchall()
        if rows:
            cols = list(rows[0]._mapping.keys())
            key = f"{table}.{column}"
            backup["dependents"][key] = [dict(zip(cols, r)) for r in rows]

    backup_path.write_text(
        json.dumps(backup, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(f"已写备份：{backup_path}（contents {len(content_rows)} 行 + "
          f"{sum(len(v) for v in backup['dependents'].values())} 依赖行）")

    # 2) 删除（单事务）：先依赖后主表；submissions 只置 NULL 不删。
    # 上面的备份 SELECT 已触发 autobegin，先 commit 掉只读事务才能 begin()。
    conn.commit()
    with conn.begin():
        for table, column in DEPENDENT_TABLES:
            if table == "content_submissions":
                conn.execute(
                    text(
                        "UPDATE content_submissions SET content_id=NULL, "
                        "applied_content_id=NULL "
                        f"WHERE content_id IN ({id_list}) OR applied_content_id IN ({id_list})"
                    )
                )
                continue
            conn.execute(text(f"DELETE FROM {table} WHERE {column} IN ({id_list})"))
        conn.execute(text(f"DELETE FROM contents WHERE id IN ({id_list})"))
    print(f"已删除 {len(ids)} 条文学作品及其依赖行（submissions 已置 NULL 保留）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理已上线的文学作品（默认只统计）")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径（绝对路径）")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认只统计）")
    parser.add_argument("--backup", help="--apply 时的备份 JSON 路径")
    args = parser.parse_args(argv)

    if args.apply and not args.backup:
        parser.error("--apply 必须搭配 --backup（先备份再删）")

    engine = create_engine(
        f"sqlite:///{args.db}", connect_args={"timeout": 60}
    )
    with engine.connect() as conn:
        ids = collect_target_ids(conn)
        if args.apply:
            return apply_mode(conn, ids, Path(args.backup))
        return stats_mode(conn, ids)


if __name__ == "__main__":
    raise SystemExit(main())
