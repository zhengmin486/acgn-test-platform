"""资讯识别回填：给库中已有资讯补「识别标签 + 关联作品」。

适用场景：
- 识别功能上线前导入的资讯没有标签与关联（article_tags / article_content_links 为空）；
- 作品库扩充（又导入了一批 Bangumi 条目）后，老资讯想按新词表重新识别（--force）；
- 某篇资讯导入时词表还是空的，事后补识别（--article-id 指定）。

设计要点（与 scripts/fetch_covers.py 同一风格）：
- 识别逻辑全部复用 app/article_tags（受控词表匹配 + 替换式写入），本脚本
  只负责「挑文章 -> 识别 -> （可选）写库 -> 汇总」，不复制识别算法；
- 默认只预览：照样做识别计算并打印结果，但不写库；显式 --apply 才写入；
- 已有识别结果的文章默认跳过（--force 才重新识别），重复运行成本低；
- 纯本地计算（词表来自库内数据），不访问网络，也没有「单条失败」概念；
- 退出码：0 正常完成；1 指定了 --article-id 但一篇都没匹配到；2 用法错误。

用法示例：
    # 预览：对所有资讯做识别计算，不写库
    python scripts/link_articles.py
    # 正式回填（写入 article_tags / article_content_links）
    python scripts/link_articles.py --apply
    # 已有识别结果的也重算（作品库扩充后刷新）
    python scripts/link_articles.py --apply --force
    # 只处理指定文章
    python scripts/link_articles.py --apply --article-id 7 8 9
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import article_tags, crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402

# 退出码：0 正常完成；1 指定文章一篇都没匹配到；2 用法错误。
EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_USAGE = 2


@dataclass
class LinkOutcome:
    """单篇资讯的识别结果。"""

    article_id: int
    title: str
    status: str  # recognized / pending / skipped
    tags: list[str] = field(default_factory=list)
    contents: list[str] = field(default_factory=list)
    reason: str = ""

    def describe(self) -> str:
        label = {
            "recognized": "已识别",
            "pending": "待识别(预览)",
            "skipped": "跳过",
        }.get(self.status, self.status)
        parts = [f"[{label}] #{self.article_id} 《{self.title}》"]
        parts.append(f"标签：{'、'.join(self.tags) if self.tags else '未识别到'}")
        parts.append(f"作品：{'、'.join(self.contents) if self.contents else '未关联到'}")
        if self.reason:
            parts.append(f"- {self.reason}")
        return " ".join(parts)


@dataclass
class LinkSummary:
    """一次回填运行的汇总统计。"""

    mode: str  # preview / apply
    requested: int = 0
    recognized: int = 0
    pending: int = 0
    skipped: int = 0
    tagged: int = 0  # 识别出至少一个标签的篇数
    linked: int = 0  # 关联到至少一部作品的篇数
    outcomes: list[LinkOutcome] = field(default_factory=list)

    def describe(self) -> str:
        mode_label = "正式回填（--apply）" if self.mode == "apply" else "预览（dry-run）"
        return "\n".join(
            [
                f"模式：{mode_label}",
                "回填汇总：",
                f"  资讯总数：{self.requested}",
                f"  实际识别：{self.recognized}（预览待识别 {self.pending}）",
                f"  跳过（已有识别结果）：{self.skipped}",
                f"  识别出标签：{self.tagged} 篇",
                f"  关联到作品：{self.linked} 篇",
            ]
        )


def list_target_articles(db, article_ids: list[int] | None) -> list[Article]:
    """挑出要处理的资讯（按 id 升序）；给了 article_ids 就只取这些。"""
    stmt = select(Article).order_by(Article.id)
    if article_ids:
        stmt = stmt.where(Article.id.in_(article_ids))
    return list(db.scalars(stmt))


def has_recognition(db, article_id: int) -> bool:
    """该资讯是否已有识别结果（标签或关联作品任一非空即算）。"""
    if crud.list_article_tags(db, article_id):
        return True
    return crud.count_article_content_links(db, article_id) > 0


def run_backfill(
    session_factory,
    *,
    apply: bool = False,
    force: bool = False,
    article_ids: list[int] | None = None,
) -> LinkSummary:
    """回填主流程：挑文章 -> 识别 -> （apply 时）写库 -> 汇总。

    force：已有识别结果的文章也重新识别（替换式写入，不会堆叠关联行）。
    article_ids：只处理指定文章（None = 全部）。
    """
    summary = LinkSummary(mode="apply" if apply else "preview")
    db = session_factory()
    try:
        articles = list_target_articles(db, article_ids)
        # 词表只查一次：标签名 + 作品标题（识别是纯本地计算，不访问网络）。
        tag_names = crud.list_all_tag_names(db)
        content_titles = [title for _, title in crud.list_content_titles(db)]

        for article in articles:
            summary.requested += 1
            recognition = article_tags.recognize(
                article, tag_names=tag_names, content_titles=content_titles
            )
            if has_recognition(db, article.id) and not force:
                summary.skipped += 1
                summary.outcomes.append(
                    LinkOutcome(
                        article.id,
                        article.title,
                        "skipped",
                        recognition.tags,
                        recognition.content_titles,
                        "已有识别结果，跳过（--force 重新识别）",
                    )
                )
                continue
            if not recognition.tags and not recognition.content_titles:
                # 识别结果为空：写与不写结果一样，按「无需写入」跳过，
                # 不重复计数（否则每次回填都把这类文章算成「实际识别」）。
                summary.skipped += 1
                summary.outcomes.append(
                    LinkOutcome(
                        article.id,
                        article.title,
                        "skipped",
                        reason="未识别到标签或作品，无需写入",
                    )
                )
                continue

            if apply:
                article_tags.apply_recognition(db, recognition)
                summary.recognized += 1
                status, reason = "recognized", "已写入"
            else:
                summary.pending += 1
                status, reason = "pending", "预览模式：未写入数据库"
            if recognition.tags:
                summary.tagged += 1
            if recognition.content_titles:
                summary.linked += 1
            summary.outcomes.append(
                LinkOutcome(
                    article.id, article.title, status, recognition.tags,
                    recognition.content_titles, reason,
                )
            )

        if apply and summary.recognized:
            db.commit()
    finally:
        db.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "给已有资讯回填「识别标签 + 关联作品」（默认只预览不写库，--apply 才写入）。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把识别结果写入 article_tags / article_content_links；不加则只预览",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有识别结果的资讯也重新识别（作品库扩充后刷新用）",
    )
    parser.add_argument(
        "--article-id",
        type=int,
        nargs="+",
        default=None,
        metavar="N",
        help="只处理指定 ID 的资讯（可写多个）；缺省处理全部资讯",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None, *, session_factory=None) -> int:
    """CLI 入口：执行一次识别回填。"""
    args = build_parser().parse_args(argv)

    if args.article_id is not None:
        if any(article_id <= 0 for article_id in args.article_id):
            print("用法错误：--article-id 必须是正整数")
            return EXIT_USAGE
        args.article_id = list(dict.fromkeys(args.article_id))  # 去重保序

    if session_factory is None:
        engine = build_engine(args.database_url)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    print(f"数据库：{args.database_url}")
    if args.article_id:
        print(f"范围：指定资讯 {args.article_id}")

    summary = run_backfill(
        session_factory,
        apply=args.apply,
        force=args.force,
        article_ids=args.article_id,
    )

    for outcome in summary.outcomes:
        print(outcome.describe())
    print(summary.describe())

    if args.article_id and not summary.requested:
        print(f"没有找到指定 ID 的资讯：{args.article_id}")
        return EXIT_NO_MATCH
    if not args.apply:
        print("模式：dry-run，未写入数据库。确认后追加 --apply 正式回填。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
