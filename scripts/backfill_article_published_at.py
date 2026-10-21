"""回填资讯的真实发布时间：重抓详情页，修正被写成「侧栏日期」的 published_at。

背景（2026-10-21）：游民星空的详情页里，「发布时间：YYYY-MM-DD」出现在**侧栏推荐
列表**中且只有日期，旧解析器把它当成了文章发布时间，于是 9 月的文章被写成抓取当天、
全部 00:00，列表页「按发布时间从新到旧」的时效完全失真。解析层已改为先读文章头部
``class="Mid2L_tit"`` 里的 ``YYYY-MM-DD HH:MM:SS``；本脚本负责把已入库的旧数据修正。

用法：
    python scripts/backfill_article_published_at.py                # 预览
    python scripts/backfill_article_published_at.py --apply        # 写入
    python scripts/backfill_article_published_at.py --source 游民星空\\ ACG --apply

幂等：只更新「重抓解析出的时间」与库里不同、且解析成功的条目；抓取失败保留原值。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import news_sources  # noqa: E402
from scripts.import_articles import fetch_page  # noqa: E402

# 只回填「详情页能给出真实时刻」的源。ANIPLEX 的详情页本身只有日期（Oct 20, 2026），
# 回填无收益，跳过。
DEFAULT_SOURCES = ("游民星空 ACG",)


@dataclass
class BackfillSummary:
    checked: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"检查 {self.checked} 条、修正 {self.updated} 条、"
            f"无需改动 {self.unchanged} 条、抓取失败 {self.failed} 条"
        )


def resolve_database_url() -> str:
    """把 settings.database_url 的相对路径解析成绝对路径（cron/系统单元的 cwd 不可靠）。"""
    raw = settings.database_url
    if raw.startswith("sqlite:///"):
        rel = raw[len("sqlite:///") :]
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel.startswith("/"):
            return "sqlite:///" + str(BASE_DIR / rel)
    return raw


def backfill(
    session_factory,
    *,
    apply: bool = False,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    fetch=None,
    sleep=time.sleep,
    delay: float = 0.3,
) -> BackfillSummary:
    """逐条重抓详情页并修正 published_at；apply=False 只统计不写库。"""
    fetch = fetch or fetch_page
    summary = BackfillSummary()
    db = session_factory()
    try:
        rows = list(
            db.scalars(
                select(Article)
                .where(Article.source.in_(sources))
                .order_by(Article.id)
            )
        )
        for article in rows:
            summary.checked += 1
            url = article.original_url
            if not url:
                summary.failed += 1
                summary.details.append({"id": article.id, "reason": "缺少 original_url"})
                continue
            try:
                html = fetch(url)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮回填
                summary.failed += 1
                summary.details.append({"id": article.id, "reason": f"抓取失败：{exc}"[:160]})
                sleep(delay)
                continue

            real = news_sources._gamersky_published_at(html) if "gamersky" in url else None
            if real is None:
                summary.failed += 1
                summary.details.append({"id": article.id, "reason": "详情页未找到发布时间"})
                sleep(delay)
                continue

            if article.published_at == real:
                summary.unchanged += 1
                sleep(delay)
                continue

            old = article.published_at
            summary.updated += 1
            summary.details.append(
                {
                    "id": article.id,
                    "old": old.strftime("%Y-%m-%d %H:%M:%S") if old else "-",
                    "new": real.strftime("%Y-%m-%d %H:%M:%S"),
                    "title": (article.title or "")[:40],
                }
            )
            if apply:
                article.published_at = real
            sleep(delay)

        if apply:
            db.commit()
        return summary
    finally:
        db.close()


def clamp_future(
    session_factory,
    *,
    apply: bool = False,
    now=None,
) -> BackfillSummary:
    """把「未来日期」的条目改成收录时间（created_at）。

    官方 PR 里的活动预告，源站给的日期是**活动举办日**（如 12-13 的见面会），
    不是发布日；照抄会让未发生的活动顶在「最新」最前面，时效就假了。
    这类条目没有可信的发布日，用「我们收录它的时间」比用一个未来日期更诚实。
    """
    from datetime import datetime as _dt

    now = now or _dt.now()
    summary = BackfillSummary()
    db = session_factory()
    try:
        rows = list(
            db.scalars(
                select(Article)
                .where(Article.published_at > now)
                .order_by(Article.published_at)
            )
        )
        for article in rows:
            summary.checked += 1
            old = article.published_at
            new = article.created_at or now
            summary.updated += 1
            summary.details.append(
                {
                    "id": article.id,
                    "old": old.strftime("%Y-%m-%d %H:%M:%S") if old else "-",
                    "new": new.strftime("%Y-%m-%d %H:%M:%S"),
                    "title": (article.title or "")[:40],
                }
            )
            if apply:
                article.published_at = new
        if apply:
            db.commit()
        return summary
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填资讯真实发布时间（重抓详情页）")
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认仅预览）")
    parser.add_argument(
        "--source",
        default=",".join(DEFAULT_SOURCES),
        help="资讯来源名，多个用逗号分隔（默认：游民星空 ACG）",
    )
    parser.add_argument("--delay", type=float, default=0.3, help="每条之间的间隔秒数")
    parser.add_argument(
        "--clamp-future",
        action="store_true",
        help="只处理「未来日期」的条目：改成收录时间（活动预告的活动日不算发布日）",
    )
    args = parser.parse_args(argv)

    sources = tuple(name.strip() for name in args.source.split(",") if name.strip())
    engine = build_engine(resolve_database_url(), busy_timeout=60)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    if args.clamp_future:
        summary = clamp_future(session_factory, apply=args.apply)
        engine.dispose()
        print("模式：'未来日期'改收录时间"
              f"（{'正式写入' if args.apply else '预览'}）")
        print(summary.describe())
        for item in summary.details[:40]:
            print(f"  #{item['id']} {item['old']} -> {item['new']} | {item['title']}")
        return 0

    summary = backfill(
        session_factory, apply=args.apply, sources=sources, delay=args.delay
    )
    engine.dispose()

    print(f"数据源：{', '.join(sources)}")
    print(f"模式：{'正式写入（--apply）' if args.apply else '预览（dry-run）'}")
    print(summary.describe())
    for item in summary.details[:40]:
        if "new" in item:
            print(f"  #{item['id']} {item['old']} -> {item['new']} | {item['title']}")
        else:
            print(f"  #{item['id']} 失败：{item['reason']}")
    if len(summary.details) > 40:
        print(f"  ……其余 {len(summary.details) - 40} 条省略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
