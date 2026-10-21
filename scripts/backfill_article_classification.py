"""回填既有资讯的分类 / 事件 / 地区 / 优先级（一次性迁移，幂等）。

2026-10 资讯重构给 articles 增加了 region / subcategory / event_type / source_type /
priority_score / is_adult 六列，并把主体类型收敛为 动画 / 漫画 / 轻小说 / 游戏 / 综合动态。
crud.ensure_article_classification_columns 只保证列存在并填默认值，本脚本按
scripts/news_classify 的同一套规则把历史行重算一遍，让旧资讯与新建资讯口径一致。

- category：旧栏目名（动漫资讯 / 活动资讯 / 榜单资讯 / 综合资讯 …）归一化到主体类型；
- region：来源为 ANIPLEX NEWS BOX 的按日本起步，其余用标题信号判定；
- source_type：ANIPLEX NEWS BOX 是官方 PR，其余按「原文标题含日文」粗判，回落媒体编译；
- priority_score / subcategory / event_type / is_adult：全部按 news_classify 重算；
- is_major：2026-10-22 新增的「重大官宣」标记，同样按 news_classify 重算，
  让历史行与新建资讯口径一致（豁免额度只统计当天入库的行，回填旧行不影响额度）。

用法（默认预览，--apply 才写库）：
    python scripts/backfill_article_classification.py
    python scripts/backfill_article_classification.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.database import build_engine, sessionmaker  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import news_classify  # noqa: E402

# 官方宣发源（旧单源导入器写入的 ANIPLEX NEWS BOX）。
PR_SOURCES = {"ANIPLEX NEWS BOX"}


def _default_region(source: str) -> str:
    """旧行没有来源地区记录，按来源粗判起步值，再由标题信号细化。"""
    return news_classify.REGION_JP if source in PR_SOURCES else news_classify.REGION_JP


def classify_row(article: Article) -> dict:
    """按与入库时一致的规则重算一条资讯的各维度。"""
    subject = news_classify.normalize_subject(article.category)
    subcategory = news_classify.classify_subcategory(
        article.title, article.summary or "", subject
    )
    event_type = news_classify.classify_event_type(article.title, article.summary or "")
    region = news_classify.detect_region(
        _default_region(article.source or ""), article.title, article.summary or ""
    )
    source_type = (
        news_classify.SOURCE_TYPE_PR
        if (article.source or "") in PR_SOURCES
        else news_classify.SOURCE_TYPE_MEDIA
    )
    return {
        "category": subject,
        "subcategory": subcategory,
        "event_type": event_type,
        "region": region,
        "source_type": source_type,
        "priority_score": news_classify.compute_priority_score(
            category=subject,
            subcategory=subcategory,
            region=region,
            event_type=event_type,
            source_type=source_type,
            text=f"{article.title}\n{article.summary or ''}",
        ),
        "is_adult": news_classify.flag_adult(article.title, article.summary or ""),
        "is_major": news_classify.is_major_announcement(
            article.title, article.summary or ""
        ),
    }


def run(database_url: str, *, apply: bool) -> dict:
    engine = build_engine(database_url, busy_timeout=60.0)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    stats = {"total": 0, "changed": 0, "adult": 0, "major": 0}
    db = factory()
    try:
        rows = list(db.scalars(select(Article)))
        stats["total"] = len(rows)
        for article in rows:
            info = classify_row(article)
            if info["is_adult"]:
                stats["adult"] += 1
            if info["is_major"]:
                stats["major"] += 1
            diff = {
                key: value
                for key, value in info.items()
                if getattr(article, key) != value
            }
            if not diff:
                continue
            stats["changed"] += 1
            if apply:
                for key, value in diff.items():
                    setattr(article, key, value)
        if apply:
            db.commit()
    finally:
        db.close()
        engine.dispose()
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填既有资讯的分类与优先级（默认预览）")
    parser.add_argument("--apply", action="store_true", help="正式写库")
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args(argv)
    stats = run(args.database_url, apply=args.apply)
    mode = "已写入" if args.apply else "预览（未写入）"
    print(
        f"{mode}：共 {stats['total']} 条资讯，需更新 {stats['changed']} 条，"
        f"其中成人向 {stats['adult']} 条、重大官宣 {stats['major']} 条"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
