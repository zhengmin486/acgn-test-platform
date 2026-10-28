"""清理已被收录规则判定为垃圾的存量资讯（2026-10-25 用户定稿口径）。

三类不要的东西：

1. **源站自营内容**（机核自办活动「核聚变」/「核电波」/「GGG」系列）——
   用户定稿「网站的自营信息我们不要」；
2. **周报 / 行业综述栏目**（触乐「本周行业大事」系列）——标题堆砌没有单一事件，
   里面提到的作品还会被判成官宣走豁免；
3. **普通游戏的联名周边**（八位堂 × 古墓丽影 三十周年联名手柄）——
   用户「普通游戏的联名不在乎」。

判定**全部复用 news_filters 的三个函数**，与抓取链路同源：规则改一处，抓取与存量
清理同时生效，不会出现「新规则只对新稿有效、脏数据留在库里」的错位。

安全设计（与 cleanup_literature.py 同一套）：
- **默认只统计**：打印命中条目与原因、依赖表行数、收藏影响；不碰任何数据。
- ``--apply`` 必须搭配 ``--backup``：先写完整行快照 JSON 再删，单事务。
- 删除范围 = 文章本体 + 标签 / 作品 / 实体关联；**收藏行按设计保留**
  （``ArticleFavorite`` 是弱关联，内连接查不到即不展示，清理由用户显式操作触发）。

用法（服务器侧，cwd 必须是 /opt/acgn-test-platform）：

    # 1) 先看影响面
    .venv/bin/python3 scripts/cleanup_news_junk.py --db /opt/acgn-test-platform/local_dev.db
    # 2) 确认后执行（自动先写备份）
    .venv/bin/python3 scripts/cleanup_news_junk.py --db /opt/acgn-test-platform/local_dev.db \\
        --apply --backup /opt/acgn-test-platform/backups/news_junk_cleanup_$(date +%Y%m%d).json
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
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from scripts import news_daily, news_filters, news_sources  # noqa: E402

# 删除时连带清理的依赖表（收藏表不在内，见文件头说明）
DEPENDENT_TABLES: list[tuple[str, str]] = [
    ("article_tags", "article_id"),
    ("article_content_links", "article_id"),
    ("article_entities", "article_id"),
]

ARTICLE_COLUMNS = [
    "id", "title", "category", "source", "source_type", "published_at",
    "created_at", "image_url", "original_url", "is_major", "priority_score",
]


def _source_key_by_label() -> dict[str, str]:
    """源名称 → 源键：Article.source 存的是标签（「机核 GCORES」），
    而自营词表按源键（``gcores``）分表。"""
    return {spec.label: spec.key for spec in news_sources.SOURCES}


class _ArticleRow:
    """判定函数要的字段容器（不建 ORM 对象：本脚本按连接 + 原生 SQL 扫库）。"""

    __slots__ = ("id", "source", "title", "summary", "category", "source_type")

    def __init__(self, article_id, source, title, summary, category, source_type):
        self.id = article_id
        self.source = source
        self.title = title
        self.summary = summary
        self.category = category
        self.source_type = source_type


def _flag_reason(article, key_by_label: dict[str, str], terms: list[str]) -> str | None:
    """一条资讯命中三条新规则中的哪一条（未命中返回 None）。"""
    source_key = key_by_label.get((article.source or "").strip(), "")
    reason = news_filters.self_op_reason(source_key, article.title, article.summary)
    if reason:
        return reason
    return news_filters.media_game_rule_reason(
        article.title,
        article.summary,
        article.category,
        article.source_type,
        terms,
    )


def collect_targets(conn, terms: list[str]) -> list[tuple[int, str, str, str]]:
    """扫描全部资讯，返回 [(id, 来源, 标题, 原因)]。"""
    key_by_label = _source_key_by_label()
    rows = conn.execute(
        text("SELECT id, source, title, summary, category, source_type "
             "FROM articles ORDER BY id")
    ).fetchall()

    hits: list[tuple[int, str, str, str]] = []
    for article_id, source, title, summary, category, source_type in rows:
        reason = _flag_reason(
            _ArticleRow(article_id, source, title, summary, category, source_type),
            key_by_label,
            terms,
        )
        if reason:
            hits.append((article_id, source or "", title, reason))
    return hits


def load_terms(conn) -> list[str]:
    """ACG 游戏词表（联名周边的 ACG 证据来源），与 news_daily 同一份。"""
    session = sessionmaker(bind=conn, expire_on_commit=False)()
    try:
        return crud.list_acgn_game_terms(
            session, manual_aliases=news_daily._load_game_ip_aliases()
        )
    finally:
        session.close()


def stats_mode(conn, hits: list[tuple[int, str, str, str]]) -> int:
    if not hits:
        print("没有命中「自营 / 周报 / 普通游戏联名」的资讯，无需清理。")
        return 0

    print(f"命中待清理资讯：{len(hits)} 条")
    for article_id, source, title, reason in hits:
        print(f"  #{article_id} [{source}] {title[:44]}\n      原因：{reason}")

    id_list = ",".join(str(i) for i, *_ in hits)
    dep_total = 0
    print("\n依赖行：")
    for table, column in DEPENDENT_TABLES:
        n = int(
            conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({id_list})")
            ).scalar_one()
        )
        if n:
            print(f"  {table}.{column}: {n} 行")
        dep_total += n
    print(f"  合计 {dep_total} 行（--apply 会一并删除）")

    fav = int(
        conn.execute(
            text(
                "SELECT COUNT(DISTINCT user_id) FROM article_favorites "
                f"WHERE article_id IN ({id_list})"
            )
        ).scalar_one()
    )
    print(f"\n收藏影响：{fav} 名用户收藏过这些资讯（收藏行按设计保留，不随文章删除）")
    print("\n以上是影响面统计（未改动任何数据）。确认后加 --apply --backup 执行。")
    return 0


def apply_mode(conn, hits: list[tuple[int, str, str, str]], backup_path: Path) -> int:
    if not hits:
        print("没有可清理的条目。")
        return 0

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    id_list = ",".join(str(i) for i, *_ in hits)
    col_sql = ", ".join(ARTICLE_COLUMNS)
    article_rows = [
        dict(zip(ARTICLE_COLUMNS, row))
        for row in conn.execute(
            text(f"SELECT {col_sql} FROM articles WHERE id IN ({id_list})")
        ).fetchall()
    ]
    backup: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": "cleanup news junk (self-op / weekly digest / non-ACG collab merch, 2026-10-25)",
        "article_ids": [i for i, *_ in hits],
        "hits": [
            {"id": i, "source": s, "title": t, "reason": r} for i, s, t, r in hits
        ],
        "articles": article_rows,
        "dependents": {},
    }
    for table, column in DEPENDENT_TABLES:
        rows = conn.execute(
            text(f"SELECT * FROM {table} WHERE {column} IN ({id_list})")
        ).fetchall()
        if rows:
            cols = list(rows[0]._mapping.keys())
            backup["dependents"][f"{table}.{column}"] = [
                dict(zip(cols, r)) for r in rows
            ]

    backup_path.write_text(
        json.dumps(backup, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(
        f"已写备份：{backup_path}（articles {len(article_rows)} 行 + "
        f"{sum(len(v) for v in backup['dependents'].values())} 依赖行）"
    )

    # 上面的备份 SELECT 已触发 autobegin，先 commit 掉只读事务才能 begin()。
    conn.commit()
    with conn.begin():
        for table, column in DEPENDENT_TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE {column} IN ({id_list})"))
        conn.execute(text(f"DELETE FROM articles WHERE id IN ({id_list})"))
    print(f"已删除 {len(hits)} 条资讯及其依赖行（收藏行保留）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理自营 / 周报 / 普通游戏联名资讯（默认只统计）")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径（绝对路径）")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认只统计）")
    parser.add_argument("--backup", help="--apply 时的备份 JSON 路径")
    args = parser.parse_args(argv)

    if args.apply and not args.backup:
        parser.error("--apply 必须搭配 --backup（先备份再删）")

    engine = create_engine(f"sqlite:///{args.db}", connect_args={"timeout": 60})
    with engine.connect() as conn:
        terms = load_terms(conn)
        hits = collect_targets(conn, terms)
        if args.apply:
            return apply_mode(conn, hits, Path(args.backup))
        return stats_mode(conn, hits)


if __name__ == "__main__":
    raise SystemExit(main())
