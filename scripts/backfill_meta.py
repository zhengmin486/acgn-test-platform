"""批量补全作品元数据：客观信息 / 制作人员 / 角色声优（空闲时慢慢跑）。

数据来源：Bangumi v0（每部 3 次请求：条目 / 角色 / 职员表），限速执行。
补全内容见 app/bangumi_meta.py：
- 媒介 / 原作载体 / 话数 / 年份 -> contents 列；
- 导演 / 音乐 / 系列构成 / 角色设计 / 原作 / 动画制作 -> 实体体系（/creators）；
- 脚本 / 副导演 / 美术监督 / 音响监督 -> content_credits；
- 原画 / 作画监督等长尾先存候选，本站累计 ≥10 部作品的自动转正展示；
- 角色-声优（只留原版）-> content_cast（/cast/{person_id}）；
- 放送星期 / 播放结束 / 官方网站 / 别名 -> content_facts。

默认只补 engaged（收藏 / 标注 / 浏览过）的作品，且跳过 30 天内已补过的；
--scope all 才全量，--force 忽略缓存时间。

用法：
    python scripts/backfill_meta.py                 # 预览：只列出要补的作品
    python scripts/backfill_meta.py --apply         # 正式补全
    python scripts/backfill_meta.py --apply --limit 20 --sleep 1.5
    python scripts/backfill_meta.py --apply --scope all --force
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

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import bangumi_meta, crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402


@dataclass
class MetaSummary:
    scanned: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def list_targets(db, *, scope: str, force: bool, limit: int | None) -> list[Content]:
    """挑出要补的作品：有 bangumi_id，且未补过 / 超过 TTL。"""
    rows = list(
        db.scalars(
            select(Content)
            .where(Content.bangumi_id.is_not(None))
            .order_by(Content.id.desc())
        )
    )
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
    sync=None,
    sleep=time.sleep,
) -> MetaSummary:
    summary = MetaSummary()
    sync = sync or bangumi_meta.sync_content_meta

    db = session_factory()
    try:
        targets = list_targets(db, scope=scope, force=force, limit=limit)
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
                )
                summary.applied += 1
                print(f"[OK] #{content.id} {content.title}")
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
                db.rollback()
                summary.failed += 1
                summary.errors.append(f"#{content.id} {content.title}: {exc}")
                print(f"[失败] #{content.id} {content.title}: {exc}")

        if apply:
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
        "--database-url", default=settings.database_url, help="数据库连接串"
    )
    return parser


def main(argv=None) -> int:  # noqa: ANN001 - CLI 入口
    args = build_parser().parse_args(argv)
    engine = build_engine(args.database_url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    summary = run(
        factory,
        apply=args.apply,
        scope=args.scope,
        force=args.force,
        limit=args.limit,
        pause=args.sleep,
    )
    mode = "APPLY" if args.apply else "PREVIEW"
    print(
        f"[{mode}] 待处理 {summary.scanned} 部，成功 {summary.applied}，失败 {summary.failed}"
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover - 手工执行入口
    raise SystemExit(main())
