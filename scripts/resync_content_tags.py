"""按 bangumi_id 重新拉取 Bangumi 条目并刷新题材标签。

背景：
- 站内 ``contents`` 只存了 ``bangumi_id``，原始 Bangumi 标签未落库；要按新口径
  （「除作品信息外标注最多的 5 个」）补齐历史作品标签，只能按 id 回源重提；
- 新口径见 ``app/tagging.is_genre_candidate``：排除公司 / 年份 / 季度 / 媒介 /
  平台 / 地区 / 元标签，其余（含未进白名单的真实主题词）按标注数取前 5 个；
- 年份只信条目 ``date``（作品信息），绝不从标签反推——与「年份按作品信息、不
  按标签」一致，避免年份栏稀疏。

数据源复用 ``app.bangumi_meta.fetch_subject_meta``（走配置里的 Bangumi 镜像，
服务器侧镜像可达；本机直连官方 API 可能被污染，失败会跳过不中断）。

默认只预览（PREVIEW），``--apply`` 才写库。写库为逐条 commit，失败的单部作品
跳过并计入 skipped，不会中断整批；推荐在服务器上对全量跑，本机只验证逻辑。

用法：
    python scripts/resync_content_tags.py                      # 预览（不写库）
    python scripts/resync_content_tags.py --apply              # 全量写库
    python scripts/resync_content_tags.py --type anime --limit 50
    python scripts/resync_content_tags.py --content-id 123 --apply
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.bangumi_meta import _clean_name, fetch_subject_meta, infobox_map  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402
from app.tagging import year_from_date  # noqa: E402
from scripts.import_bangumi import _subject_tags  # noqa: E402


@dataclass
class ResyncSummary:
    scanned: int = 0
    changed: int = 0
    applied: int = 0
    year_filled: int = 0
    skipped: int = 0
    fetch_failed: int = 0
    tag_deltas: list[tuple[int, str, int, int]] = field(default_factory=list)  # (id, title, before, after)


def resync(
    db,
    *,
    apply: bool,
    limit: int | None = None,
    content_id: int | None = None,
    content_type: str | None = None,
    timeout: float = 12.0,
    retries: int = 2,
    delay: float = 0.15,
) -> ResyncSummary:
    """逐个作品回源重提标签；apply=False 只算不写。"""
    summary = ResyncSummary()
    stmt = select(Content).where(Content.bangumi_id.isnot(None))
    if content_id is not None:
        stmt = stmt.where(Content.id == content_id)
    if content_type:
        stmt = stmt.where(Content.type == content_type)
    stmt = stmt.order_by(Content.id)
    if limit:
        stmt = stmt.limit(limit)

    rows = list(db.scalars(stmt))
    summary.scanned = len(rows)
    print(f"待处理作品 {summary.scanned} 部（有 bangumi_id）")

    for content in rows:
        bgm_id = int(content.bangumi_id)
        try:
            meta = fetch_subject_meta(
                bgm_id, timeout=timeout, retries=retries, sleep=time.sleep
            )
        except Exception as exc:  # noqa: BLE001 - 单部失败不应中断整批
            summary.fetch_failed += 1
            print(f"  ! #{content.id} {content.title} 拉取失败：{exc!r}")
            continue

        subject = meta.get("subject") or {}
        # 本作应排除的「作品信息」词（作者 / 声优 / 公司 / 自身译名），避免它们
        # 混入题材标签云（与「作者声优公司不进标签」一致）：
        # 1) characters / persons 接口里的人物名（anibt 镜像该接口常返回空，仅兜底）；
        # 2) infobox 里的主创 / 公司名（导演 / 音乐 / 动画制作 …，anibt 的
        #    /v0/subjects 返回齐全，是可靠来源）；
        # 3) 作品自身标题 / 别名（自身译名当标签对跨作品筛选无意义）。
        excluded: set[str] = set()
        for bucket in ("characters", "persons"):
            for item in meta.get(bucket) or []:
                if isinstance(item, dict):
                    for key in ("name", "actor", "person"):
                        value = item.get(key)
                        if value:
                            excluded.add(str(value).strip())
        info = infobox_map(subject)
        for role in (
            "导演", "系列构成", "音乐", "人物设定", "原作", "脚本",
            "美术监督", "音响监督", "总作画监督", "作画监督", "动画制作",
            "企画", "制作",
        ):
            for raw in info.get(role, []):
                name = _clean_name(str(raw))
                if name:
                    excluded.add(name)
        for raw in (subject.get("name"), subject.get("name_cn")):
            if raw:
                excluded.add(str(raw).strip())
        for raw in info.get("别名", []):
            name = _clean_name(str(raw))
            if name:
                excluded.add(name)
        new_tags = _subject_tags(subject, content.type, exclude=excluded)
        old_tags = crud.list_content_tags(db, content.id)

        # 年份只从条目 date 补（作品信息），不碰标签。
        new_year = year_from_date(str(subject.get("date") or ""))
        year_changed = bool(new_year) and content.year is None

        if new_tags == old_tags and not year_changed:
            continue

        summary.changed += 1
        summary.tag_deltas.append(
            (content.id, content.title, len(old_tags), len(new_tags))
        )
        if year_changed:
            summary.year_filled += 1
        print(
            f"  #{content.id} {content.title} 标签 {len(old_tags)}→{len(new_tags)}"
            + (f"，补年份 {new_year}" if year_changed else "")
            + ("" if apply else "  [预览]")
        )

        if not apply:
            continue

        # 与线上服务共库，写锁等待给足 60s；单部写库失败只跳过并计数，
        # 不让整批（systemd 托管跑几小时）因一部作品的中断而前功尽弃。
        try:
            crud.replace_content_tags(db, content.id, new_tags)
            if year_changed:
                content.year = new_year
                db.commit()
        except Exception as exc:  # noqa: BLE001 - 单部写库失败不应中断整批
            db.rollback()
            summary.skipped += 1
            print(f"  ! #{content.id} {content.title} 写库失败：{exc!r}")
            continue
        summary.applied += 1

        if delay:
            time.sleep(delay)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 bangumi_id 重新拉取条目并刷新题材标签（默认只预览）。"
    )
    parser.add_argument("--apply", action="store_true", help="写库；不加只预览")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少部作品")
    parser.add_argument("--content-id", type=int, default=None, help="只处理指定作品 id")
    parser.add_argument(
        "--type",
        default=None,
        help="只处理指定类型（anime/comic/game/novel）",
    )
    parser.add_argument("--timeout", type=float, default=12.0, help="单请求超时（秒）")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="每部作品之间的间隔（秒），用于礼貌限速；设为 0 关闭",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None) -> int:  # noqa: ANN001 - CLI 入口
    args = build_parser().parse_args(argv)
    # 与线上 uvicorn 共用一个库文件：写锁等待给到 60s，避免全量跑期间
    # 详情页补全 / cron 持锁时单部写入即失败。
    engine = build_engine(args.database_url, busy_timeout=60.0)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        summary = resync(
            db,
            apply=args.apply,
            limit=args.limit,
            content_id=args.content_id,
            content_type=args.type,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
        )
    finally:
        db.close()

    mode = "APPLY" if args.apply else "PREVIEW"
    print(
        f"[{mode}] 扫描 {summary.scanned} 部，变更 {summary.changed} 部"
        + (f"，写入 {summary.applied} 部" if args.apply else "")
        + f"，补年份 {summary.year_filled} 部，拉取失败 {summary.fetch_failed} 部"
    )
    if not args.apply:
        print("预览模式未写库；确认无误后加 --apply")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工执行入口
    raise SystemExit(main())
