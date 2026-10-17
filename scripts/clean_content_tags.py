"""清洗已入库作品的标签：题材留在 content_tags，公司进 content_studios，年份进 contents.year。

背景：
- 早期同步 / 导入把 Bangumi 的 top 标签原样写进 `content_tags`，里面混着
  `2011`（年份）、`A-1 Pictures`（公司）、`花泽香菜`（声优）、`TV`（媒介）；
- 标签云与「按标签筛选」只该看到题材，否则点 `2011` 会出来一堆不相干作品；
- 但年份和公司有价值，不能删：分别落到 `contents.year` 与 `content_studios`。

规则（与 app/tagging.py 同一套）：
- 题材 → 保留在 content_tags；
- 公司 → 规范名写入 content_studios（京都动画 / A-1 Pictures …）；
- 年份 → 优先级：已有 year > Bangumi 快照 date > 简介里的「YYYY 年」> 标签里的四位年份；
- 其余（声优 / 载体 / 平台 / 地区）→ 丢弃，不再关联。

默认只预览，`--apply` 才写库；写完默认清理无人引用的孤儿标签行。

用法：
    python scripts/clean_content_tags.py                  # 预览
    python scripts/clean_content_tags.py --apply          # 正式清洗
    python scripts/clean_content_tags.py --apply --limit 50
    python scripts/clean_content_tags.py --content-id 123 --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import BangumiCollection, Content, ContentTag, Tag  # noqa: E402
from app.tagging import split_tags, year_from_date  # noqa: E402


@dataclass
class CleanOutcome:
    content_id: int
    title: str
    action: str  # clean / unchanged / skip
    reason: str = ""


@dataclass
class CleanSummary:
    scanned: int = 0
    changed: int = 0
    applied: int = 0
    dropped_tags: int = 0
    studios_added: int = 0
    years_set: int = 0
    pruned_tags: int = 0
    outcomes: list[CleanOutcome] = field(default_factory=list)


def _snapshot_years(db) -> dict[int, int]:
    """bangumi_id -> 发行年（取收藏快照里的 date，多个账号同一作品取首个非空）。"""
    years: dict[int, int] = {}
    for bangumi_id, date in db.execute(
        select(BangumiCollection.subject_id, BangumiCollection.date)
    ):
        if bangumi_id in years:
            continue
        year = year_from_date(date)
        if year:
            years[int(bangumi_id)] = year
    return years


def clean_contents(
    db,
    *,
    apply: bool = False,
    limit: int | None = None,
    content_id: int | None = None,
    prune_tags: bool = True,
) -> CleanSummary:
    """逐个作品归类标签；apply=False 只算不写。"""
    summary = CleanSummary()
    stmt = select(Content).order_by(Content.id)
    if content_id is not None:
        stmt = stmt.where(Content.id == content_id)
    if limit:
        stmt = stmt.limit(limit)

    snapshot_years = _snapshot_years(db)

    for content in db.scalars(stmt):
        summary.scanned += 1
        tags = crud.list_content_tags(db, content.id)
        studios = crud.list_content_studios(db, content.id)

        # 已在 content_studios 的公司也参与去重（重复执行不会丢）
        genres, tag_studios, tag_year = split_tags(tags + studios)
        # 年份只信结构化来源：已有 year > Bangumi 快照 date > 标签里的四位年份。
        # 不从简介里正则抓年份——原作简介常出现剧情设定的年份（例如 EVA 的 2000 年），
        # 那不是发行年，写进去就是错数据。
        if content.bangumi_id is None:
            year = content.year
        else:
            year = (
                content.year
                or snapshot_years.get(int(content.bangumi_id))
                or tag_year
            )

        tags_changed = genres != tags
        studios_changed = tag_studios != studios
        year_changed = bool(year) and content.year != year

        if not (tags_changed or studios_changed or year_changed):
            summary.outcomes.append(
                CleanOutcome(content.id, content.title, "unchanged")
            )
            continue

        dropped = [name for name in tags if name not in genres]
        summary.changed += 1
        summary.dropped_tags += len(dropped)
        summary.studios_added += len([s for s in tag_studios if s not in studios])
        summary.years_set += 1 if year_changed else 0
        summary.outcomes.append(
            CleanOutcome(
                content.id,
                content.title,
                "clean" if apply else "preview",
                f"题材 {len(genres)} 个；公司 {', '.join(tag_studios) or '无'}；"
                f"年份 {year or '未知'}；丢弃 {', '.join(dropped) or '无'}",
            )
        )

        if not apply:
            continue

        if tags_changed:
            crud.replace_content_tags(db, content.id, genres)
        if studios_changed:
            crud.replace_content_studios(db, content.id, tag_studios)
        if year_changed:
            content.year = year
            db.commit()
        summary.applied += 1

    if apply and prune_tags:
        summary.pruned_tags = _prune_orphan_tags(db)

    return summary


def _prune_orphan_tags(db) -> int:
    """删除没有任何作品引用的标签行（噪音标签被移除后留下）。"""
    orphan_ids = [
        int(tag_id)
        for tag_id in db.scalars(
            select(Tag.id).where(
                Tag.id.not_in(select(ContentTag.tag_id).distinct())
            )
        )
    ]
    if not orphan_ids:
        return 0
    db.execute(delete(Tag).where(Tag.id.in_(orphan_ids)))
    db.commit()
    return len(orphan_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清洗作品标签：题材留在标签表，公司/年份拆到独立字段（默认只预览）。"
    )
    parser.add_argument(
        "--apply", action="store_true", help="写库；不加只预览会改哪些作品"
    )
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个作品")
    parser.add_argument("--content-id", type=int, default=None, help="只处理指定作品 id")
    parser.add_argument(
        "--keep-orphan-tags",
        action="store_true",
        help="不清理清洗后无人引用的标签行（默认清理）",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None) -> int:  # noqa: ANN001 - CLI 入口
    args = build_parser().parse_args(argv)
    engine = build_engine(args.database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        summary = clean_contents(
            db,
            apply=args.apply,
            limit=args.limit,
            content_id=args.content_id,
            prune_tags=not args.keep_orphan_tags,
        )
    finally:
        db.close()

    mode = "APPLY" if args.apply else "PREVIEW"
    print(f"[{mode}] 扫描 {summary.scanned} 部，需要清洗 {summary.changed} 部")
    if args.apply:
        print(
            f"写入 {summary.applied} 部；新增公司 {summary.studios_added} 条、"
            f"补年份 {summary.years_set} 部、移除噪音标签关联 {summary.dropped_tags} 条、"
            f"清理孤儿标签 {summary.pruned_tags} 行"
        )
    else:
        print("预览模式未写库；确认无误后加 --apply")
    for outcome in summary.outcomes:
        if outcome.action == "unchanged":
            continue
        print(f"  #{outcome.content_id} {outcome.title} [{outcome.action}] {outcome.reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工执行入口
    raise SystemExit(main())
