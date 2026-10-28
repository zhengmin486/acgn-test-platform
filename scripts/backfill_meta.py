"""批量补全作品元数据：客观信息 / 制作人员 / 角色声优（空闲时慢慢跑）。

数据来源：Bangumi v0（每部 2~3 次请求：条目 / 角色 / 职员表），限速执行。
补全内容见 app/bangumi_meta.py：
- 媒介 / 原作载体 / 话数 / 年份 -> contents 列；
- 导演 / 音乐 / 系列构成 / 角色设计 / 原作 / 动画制作 -> 实体体系（/creators）；
- infobox 全部人事职位（导演 / 系列构成 / 脚本 / 人物设定 / 美术监督 …）
  -> content_credits（详情页 Staff 表，2026-10-22 起不再只收 4 个边角职位）；
- 原画 / 作画监督等长尾先存候选，本站累计 ≥10 部作品的自动转正展示；
- 角色-声优（只留原版，按主角 > 配角 > 旁白 > 闲角排序）-> content_cast；
- 放送星期 / 播放结束 / 官方网站 / 别名 -> content_facts。

默认只补 engaged（收藏 / 标注 / 浏览过）的作品，且跳过 30 天内已补过的；
--scope all 才全量，--force 忽略缓存时间。

Staff 表改造后老数据只有「副导演 / 脚本 / 美术监督 / 音响监督」这几条，
要补全主创必须重刷：``--scope all --force --only-missing-staff`` 只挑 Staff
明显不全的（main 少于 --min-main 条），避免对已经刷过的作品重复发请求；
``--light`` 跳过 /persons 长尾接口（少 1/3 请求），批量回填主创足够用。

用法：
    python scripts/backfill_meta.py                 # 预览：只列出要补的作品
    python scripts/backfill_meta.py --apply         # 正式补全
    python scripts/backfill_meta.py --apply --limit 20 --sleep 1.5
    python scripts/backfill_meta.py --apply --scope all --force
    # 只补 Staff 不全的作品，按评分从高到低，先刷热门
    python scripts/backfill_meta.py --apply --scope all --force \\
        --only-missing-staff --order rating --light --sleep 0.2
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import bangumi_meta, crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content, ContentCredit  # noqa: E402


@dataclass
class MetaSummary:
    scanned: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def count_main_credits(db) -> dict[int, int]:
    """每部作品已有的 main 档 Staff 条数（判断 Staff 是否还没补全）。"""
    rows = db.execute(
        select(ContentCredit.content_id, func.count())
        .where(ContentCredit.tier == "main")
        .group_by(ContentCredit.content_id)
    ).all()
    return {int(content_id): int(count) for content_id, count in rows}


def list_targets(
    db,
    *,
    scope: str,
    force: bool,
    limit: int | None,
    order: str = "id",
    types: tuple[str, ...] = (),
    min_rating: float = 0.0,
    only_missing_staff: bool = False,
    min_main: int = 3,
) -> list[Content]:
    """挑出要补的作品：有 bangumi_id，且未补过 / 超过 TTL / Staff 不全。"""
    stmt = select(Content).where(Content.bangumi_id.is_not(None))
    if types:
        stmt = stmt.where(Content.type.in_(types))
    if min_rating > 0:
        stmt = stmt.where(Content.rating >= min_rating)
    if order == "rating":
        stmt = stmt.order_by(Content.rating.desc(), Content.id.desc())
    else:
        stmt = stmt.order_by(Content.id.desc())
    rows = list(db.scalars(stmt))
    if not force:
        cutoff = datetime.now() - timedelta(days=settings.bangumi_meta_ttl_days)
        rows = [
            row
            for row in rows
            if row.meta_fetched_at is None or row.meta_fetched_at < cutoff
        ]
    if scope == "engaged":
        allowed = crud.list_engaged_content_ids(db)
        rows = [row for row in rows if row.id in allowed]
    if only_missing_staff:
        counts = count_main_credits(db)
        rows = [row for row in rows if counts.get(row.id, 0) < min_main]
    if limit:
        rows = rows[:limit]
    return rows


def run(
    session_factory,
    *,
    apply: bool = False,
    scope: str = "engaged",
    force: bool = False,
    limit: int | None = None,
    pause: float = 1.0,
    order: str = "id",
    types: tuple[str, ...] = (),
    min_rating: float = 0.0,
    only_missing_staff: bool = False,
    min_main: int = 3,
    with_persons: bool = True,
    sync=None,
    sleep=time.sleep,
) -> MetaSummary:
    summary = MetaSummary()
    sync = sync or bangumi_meta.sync_content_meta

    db = session_factory()
    try:
        targets = list_targets(
            db,
            scope=scope,
            force=force,
            limit=limit,
            order=order,
            types=types,
            min_rating=min_rating,
            only_missing_staff=only_missing_staff,
            min_main=min_main,
        )
        for index, content in enumerate(targets):
            summary.scanned += 1
            if not apply:
                print(f"[预览] #{content.id} {content.title}（bangumi #{content.bangumi_id}）")
                continue
            if index and pause > 0:
                sleep(pause)
            try:
                sync(
                    db,
                    content,
                    proxy=settings.bangumi_proxy,
                    timeout=settings.bangumi_timeout,
                    retries=2,
                    sleep=sleep,
                    with_persons=with_persons,
                )
                summary.applied += 1
                print(f"[OK] #{content.id} {content.title}")
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
                db.rollback()
                summary.failed += 1
                summary.errors.append(f"#{content.id} {content.title}: {exc}")
                print(f"[失败] #{content.id} {content.title}: {exc}")

        if apply and with_persons:
            # 整批补完后统一检查一次转正（单部同步时只查本次涉及的人）
            promoted = crud.promote_staff_candidates(
                db, threshold=bangumi_meta.LONG_TIER_MIN_WORKS
            )
            if promoted:
                print(f"[转正] {promoted} 条长尾职员达到 {bangumi_meta.LONG_TIER_MIN_WORKS} 部作品，已展示")
    finally:
        db.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="补全作品客观信息 / 制作人员 / 角色声优（默认预览，--apply 才写库）。"
    )
    parser.add_argument("--apply", action="store_true", help="写库；不加只列出待补作品")
    parser.add_argument(
        "--scope",
        choices=("engaged", "all"),
        default="engaged",
        help="engaged=收藏/标注/浏览过的（默认）；all=全量",
    )
    parser.add_argument("--force", action="store_true", help="忽略 30 天缓存，重新拉取")
    parser.add_argument("--limit", type=int, default=None, help="本次最多补多少部")
    parser.add_argument("--sleep", type=float, default=1.0, help="每部之间的停顿秒数（默认 1）")
    parser.add_argument(
        "--order",
        choices=("id", "rating"),
        default="id",
        help="处理顺序：id=新入库优先；rating=高分优先（刷热门详情页用）",
    )
    parser.add_argument(
        "--type",
        default="",
        help="只处理指定类型（逗号分隔：anime,comic,game,novel），空=全部",
    )
    parser.add_argument("--min-rating", type=float, default=0.0, help="只处理评分不低于该值的作品")
    parser.add_argument(
        "--only-missing-staff",
        action="store_true",
        help="只处理 Staff 主创少于 --min-main 条的作品（重刷结构化 Staff 用）",
    )
    parser.add_argument(
        "--min-main",
        type=int,
        default=3,
        help="配合 --only-missing-staff：main 档 Staff 少于该条数视为未补全（默认 3）",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="跳过 /persons 长尾职员接口（少 1/3 请求；只补主创与角色足够）",
    )
    parser.add_argument(
        "--database-url", default=settings.database_url, help="数据库连接串"
    )
    return parser


def main(argv=None) -> int:  # noqa: ANN001 - CLI 入口
    args = build_parser().parse_args(argv)
    engine = build_engine(args.database_url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    types = tuple(item.strip() for item in (args.type or "").split(",") if item.strip())
    summary = run(
        factory,
        apply=args.apply,
        scope=args.scope,
        force=args.force,
        limit=args.limit,
        pause=args.sleep,
        order=args.order,
        types=types,
        min_rating=args.min_rating,
        only_missing_staff=args.only_missing_staff,
        min_main=args.min_main,
        with_persons=not args.light,
    )
    mode = "APPLY" if args.apply else "PREVIEW"
    print(
        f"[{mode}] 待处理 {summary.scanned} 部，成功 {summary.applied}，失败 {summary.failed}"
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover - 手工执行入口
    raise SystemExit(main())
