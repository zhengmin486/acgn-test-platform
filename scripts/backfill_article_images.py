"""资讯图片回填：给功能上线前入库的资讯补「正文图 + 头图」。

适用场景：
- 资讯图片功能上线前导入的文章，正文被剥成纯文本（没有 ``[图]`` 行）、
  也没有头图列 —— 本脚本重新抓一次详情页，把图片补回来；
- 想给某几篇手工补图（--article-id 指定）。

设计要点（与 scripts/link_articles.py / fetch_covers.py 同一风格）：
- 解析与下载全部复用既有实现（``import_articles.parse_detail_page`` +
  ``app.article_media``），本脚本只负责「挑文章 -> 抓详情 -> 本地化 -> 写库 -> 汇总」；
- 默认只预览：照样抓页面、算结果，但不下载图、不写库；显式 ``--apply`` 才落盘/写库；
- **只补缺图文章**：正文里已有 ``[图]`` 行或已有头图的文章默认跳过（--force 重做）；
- 详情页里没有图片的文章标记 ``no_image``，**不改动它原有的正文**（避免用新解析结果
  覆盖掉线上已有内容）；
- 退出码：0 正常完成；1 指定了 --article-id 但一篇都没匹配到；2 用法错误。

用法示例：
    # 预览：看看哪些文章能补图、能补几张（不下载、不写库）
    python3 scripts/backfill_article_images.py
    # 正式回填（下载图片落盘 + 更新 body / image_url）
    python3 scripts/backfill_article_images.py --apply
    # 只处理前 5 篇 / 指定文章
    python3 scripts/backfill_article_images.py --apply --limit 5
    python3 scripts/backfill_article_images.py --apply --article-id 3 8
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import article_body, article_media  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import import_articles  # noqa: E402

# 退出码：0 正常完成；1 指定文章一篇都没匹配到；2 用法错误。
EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_USAGE = 2

DEFAULT_SLEEP = 0.5  # 逐篇抓详情页之间的间隔（秒），对源站保持礼貌

_DETAIL_ID_RE = re.compile(r"detail\.html\?id=(\d+)")


@dataclass
class BackfillOutcome:
    """单篇资讯的回填结果。"""

    article_id: int
    title: str
    status: str  # pending / updated / skipped / no_image / failed
    images: int = 0
    reason: str = ""

    def describe(self) -> str:
        label = {
            "pending": "待补图(预览)",
            "updated": "已补图",
            "skipped": "跳过",
            "no_image": "无图",
            "failed": "失败",
        }.get(self.status, self.status)
        parts = [f"[{label}] #{self.article_id} 《{self.title}》"]
        if self.images:
            parts.append(f"图片 {self.images} 张")
        if self.reason:
            parts.append(f"- {self.reason}")
        return " ".join(parts)


@dataclass
class BackfillSummary:
    """一次回填运行的汇总统计。"""

    mode: str  # preview / apply
    requested: int = 0
    pending: int = 0
    updated: int = 0
    skipped: int = 0
    no_image: int = 0
    failed: int = 0
    images: int = 0  # 实际本地化成功的图片数（apply 时）
    outcomes: list[BackfillOutcome] = field(default_factory=list)

    def describe(self) -> str:
        mode_label = "正式回填（--apply）" if self.mode == "apply" else "预览（dry-run）"
        return "\n".join(
            [
                f"模式：{mode_label}",
                "资讯图片回填汇总：",
                f"  待处理资讯：{self.requested}",
                f"  已补图：{self.updated}（预览待补 {self.pending}）",
                f"  跳过（已有图片）：{self.skipped}",
                f"  源站无图：{self.no_image}",
                f"  抓取失败：{self.failed}",
                f"  本地化图片：{self.images} 张",
            ]
        )


def has_images(article: Article) -> bool:
    """该文章是否已经有图（头图存在，或正文里有 [图] 行）。"""
    if article.image_url:
        return True
    return bool(article_body.extract_image_urls(article.body))


def detail_id_from_url(url: str | None) -> str:
    """从 original_url 取详情页 id；不是站内详情页（如活动外链）返回空串。"""
    match = _DETAIL_ID_RE.search(url or "")
    return match.group(1) if match else ""


def list_target_articles(db, article_ids: list[int] | None, limit: int | None):
    """按 id 升序挑资讯；给了 article_ids 只取这些，limit 截断。"""
    stmt = select(Article).order_by(Article.id)
    if article_ids:
        stmt = stmt.where(Article.id.in_(article_ids))
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def run_backfill(
    session_factory,
    *,
    apply: bool = False,
    force: bool = False,
    limit: int | None = None,
    article_ids: list[int] | None = None,
    sleep=time.sleep,
    sleep_seconds: float = DEFAULT_SLEEP,
    fetch=None,
    downloader=None,
) -> BackfillSummary:
    """回填主流程：挑文章 -> 抓详情页 -> 本地化图片 -> （apply 时）写库 -> 汇总。

    fetch：可注入的页面抓取函数 ``fetch(url) -> html``（测试用；默认走真实网络）；
    downloader：可注入的图片下载器 ``downloader(url, key, index) -> 站内路径``。
    """
    summary = BackfillSummary(mode="apply" if apply else "preview")
    fetch = fetch or import_articles.fetch_page
    db = session_factory()
    try:
        articles = list_target_articles(db, article_ids, limit)
        if article_ids:
            found = {article.id for article in articles}
            missing = [str(i) for i in article_ids if i not in found]
            if missing:
                summary.outcomes.append(
                    BackfillOutcome(0, "—", "skipped", reason=f"未找到文章：{', '.join(missing)}")
                )

        first = True
        for article in articles:
            summary.requested += 1
            if has_images(article) and not force:
                summary.skipped += 1
                summary.outcomes.append(
                    BackfillOutcome(
                        article.id, article.title, "skipped",
                        reason="已有图片，跳过（--force 重做）",
                    )
                )
                continue
            detail_id = detail_id_from_url(article.original_url)
            if not detail_id:
                summary.skipped += 1
                summary.outcomes.append(
                    BackfillOutcome(
                        article.id, article.title, "skipped",
                        reason="来源不是站内详情页（如活动外链），无法补图",
                    )
                )
                continue

            if not first:
                sleep(sleep_seconds)
            first = False

            entry = import_articles.NewsEntry(
                detail_id=detail_id,
                url=article.original_url or "",
                title=article.title,
                published_at=article.published_at,
            )
            try:
                html = fetch(entry.url)
                parsed = import_articles.parse_detail_page(html, entry)
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响整体
                summary.failed += 1
                summary.outcomes.append(
                    BackfillOutcome(article.id, article.title, "failed", reason=f"抓取失败：{exc}")
                )
                continue

            body_images = article_body.extract_image_urls(parsed.body)
            if not body_images and not parsed.thumb_url:
                summary.no_image += 1
                summary.outcomes.append(
                    BackfillOutcome(
                        article.id, article.title, "no_image",
                        reason="详情页没有图片，保持原正文不变",
                    )
                )
                continue

            expected = len(body_images) + (1 if parsed.thumb_url else 0)
            if not apply:
                summary.pending += 1
                summary.outcomes.append(
                    BackfillOutcome(
                        article.id, article.title, "pending", images=expected,
                        reason="预览模式：未下载图片、未写库",
                    )
                )
                continue

            localized = article_media.localize_media(
                parsed.body, parsed.thumb_url or None,
                key=detail_id, downloader=downloader,
            )
            # 只更新有图的部分：正文用重新解析的结果（含 [图] 行），头图用本地化结果；
            # 一张都没下载成功且原本就没有头图时，仍写入（占位行保留外链，页面照样能显示）。
            article.body = localized.body
            article.image_url = localized.head_image
            summary.updated += 1
            summary.images += localized.saved
            reason = f"已写入，图片本地化 {localized.saved} 张"
            if localized.failed:
                reason += f"（{localized.failed} 张保留外链）"
            summary.outcomes.append(
                BackfillOutcome(article.id, article.title, "updated",
                                images=localized.saved, reason=reason)
            )

        if apply and summary.updated:
            db.commit()
    finally:
        db.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description="给缺图的资讯回填正文图片与头图（默认只预览，--apply 才下载并写库）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="正式回填：下载图片落盘并更新 body / image_url",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有图片的文章也重新抓取并覆盖",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="最多处理多少篇（默认全部）",
    )
    parser.add_argument(
        "--article-id",
        type=int,
        nargs="+",
        default=None,
        metavar="ID",
        help="只处理指定资讯 id（可多个）",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help=f"逐篇抓取间隔秒数（默认 {DEFAULT_SLEEP:g}）",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="数据库连接 URL；默认读取应用配置 DATABASE_URL（本地默认 SQLite）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须是正整数")

    url = args.database_url or settings.database_url
    engine = build_engine(url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    summary = run_backfill(
        session_factory,
        apply=args.apply,
        force=args.force,
        limit=args.limit,
        article_ids=args.article_id,
        sleep_seconds=args.sleep,
    )
    for outcome in summary.outcomes:
        print(outcome.describe())
    print()
    print(summary.describe())

    if args.article_id and summary.requested == 0:
        return EXIT_NO_MATCH
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
