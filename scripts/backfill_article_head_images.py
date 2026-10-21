"""回填资讯头图：给「没有图」的资讯抓详情页的 og:image 补一张。

背景（2026-10-21）：日语 RSS（4Gamer / アニメ！アニメ！）的 feed 里不带图，导入时
又没有抓详情页，于是这批资讯在列表里全是空白卡片（当时 75/237 无图）。现在导入链路
会在缺图时读详情页的 og:image；本脚本负责把已经入库的那批补齐。

策略（保守）：
- 只处理 ``image_url`` 为空的文章，已有图一律不动（要重做用 --force 也不行，
  避免覆盖线上已有的本地化图片）；
- 详情页优先级：og:image / twitter:image → 页面第一张 <img>（兜底）；
- 图**下载到本地**（与导入同一套 app.article_media.download_image），失败保留空，
  下次运行会再试（幂等）；
- 默认预览：只抓页面、算结果，不下载不写库；``--apply`` 才落盘 + 写库。

用法：
    python scripts/backfill_article_head_images.py                  # 预览
    python scripts/backfill_article_head_images.py --apply          # 写入
    python scripts/backfill_article_head_images.py --apply --limit 20
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import article_media  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import news_sources  # noqa: E402
from scripts.import_articles import fetch_page  # noqa: E402

DEFAULT_SLEEP = 0.3


@dataclass
class Outcome:
    article_id: int
    title: str
    status: str  # filled / failed / skipped
    reason: str = ""


@dataclass
class Summary:
    mode: str
    requested: int = 0
    filled: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"模式：{'正式写入（--apply）' if self.mode == 'apply' else '预览（dry-run）'}\n"
            f"待补文章：{self.requested} 篇\n"
            f"  补到图：{self.filled} 篇\n"
            f"  找不到图：{self.failed} 篇\n"
            f"  跳过：{self.skipped} 篇"
        )


def resolve_database_url() -> str:
    """相对 sqlite 路径按项目根解析（cron / systemd 的 cwd 不可靠）。"""
    raw = settings.database_url
    if raw.startswith("sqlite:///"):
        rel = raw[len("sqlite:///") :]
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel.startswith("/"):
            return "sqlite:///" + str(PROJECT_ROOT / rel)
    return raw


def media_key(url: str) -> str:
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16]


def run_backfill(
    session_factory,
    *,
    apply: bool = False,
    limit: int | None = None,
    sleep=time.sleep,
    sleep_seconds: float = DEFAULT_SLEEP,
    fetch=None,
    downloader=None,
) -> Summary:
    """逐条给缺图资讯补头图；apply=False 时只预览。"""
    summary = Summary(mode="apply" if apply else "preview")
    fetch = fetch or fetch_page
    download = downloader or (
        lambda url, key, index: article_media.download_image(url, key=key, index=index)
    )
    db = session_factory()
    try:
        stmt = select(Article).where(Article.image_url.is_(None)).order_by(Article.id)
        targets = list(db.scalars(stmt))
        if limit:
            targets = targets[:limit]
        summary.requested = len(targets)

        first = True
        for article in targets:
            if not article.original_url:
                summary.skipped += 1
                summary.outcomes.append(Outcome(article.id, article.title, "skipped", "缺少 original_url"))
                continue
            if not first:
                sleep(sleep_seconds)
            first = False

            try:
                html = fetch(article.original_url)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断
                summary.failed += 1
                summary.outcomes.append(
                    Outcome(article.id, article.title, "failed", f"抓取失败：{exc}"[:120])
                )
                continue

            image = news_sources.parse_og_image(html)
            if not image:
                summary.failed += 1
                summary.outcomes.append(
                    Outcome(article.id, article.title, "failed", "详情页没有 og:image")
                )
                continue

            if not apply:
                summary.filled += 1
                summary.outcomes.append(Outcome(article.id, article.title, "filled", image))
                continue

            try:
                path = download(image, media_key(article.original_url), 1)
            except Exception as exc:  # noqa: BLE001 - 下载失败保留外链也可接受
                summary.failed += 1
                summary.outcomes.append(
                    Outcome(article.id, article.title, "failed", f"下载失败：{exc}"[:120])
                )
                continue

            article.image_url = path
            summary.filled += 1
            summary.outcomes.append(Outcome(article.id, article.title, "filled", path))

        if apply:
            db.commit()
        return summary
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="给缺图资讯补头图（og:image）")
    parser.add_argument("--apply", action="store_true", help="下载并写库（默认只预览）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少篇")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="每条间隔秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = build_engine(resolve_database_url(), busy_timeout=60)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    summary = run_backfill(
        session_factory, apply=args.apply, limit=args.limit, sleep_seconds=args.sleep
    )
    engine.dispose()

    print(summary.describe())
    for item in summary.outcomes[:30]:
        print(f"  #{item.article_id} [{item.status}] {(item.title or '')[:30]} | {item.reason}")
    if len(summary.outcomes) > 30:
        print(f"  ……其余 {len(summary.outcomes) - 30} 条省略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
