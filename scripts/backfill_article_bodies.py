"""回填资讯正文：把「正文退化成摘要」的存量文章重抓详情页补真正文（幂等）。

2026-10-22 背景：news_sources.extract_body 原来只认 `<article>` / `mce-content-body`，
ACGCH 的 `div.article-body`、4Gamer 的 `div.entry-content` 都没命中，于是正文长度
不足阈值 → 返回空串 → `_build_body` 回落到 RSS 摘要。表现就是详情页只有一两句话。
提取器修好后（多容器 + 标签深度切片 + 取最长），存量文章需要重抓一遍。

判定「需要补」的口径：`len(body) < MIN_BODY`（默认 200）——已抓到正文的（LN 3.5k、
Aniplex 1.3k）不动；短讯本来就短，重抓后若没有更长正文也不会改动（幂等）。

只替换 body，**不动 summary**：summary 是 feed 提供的摘要，详情页模板的 lead 逻辑
（business_pages.article_detail_lead）靠两者比对决定是否重复展示。

用法（默认预览，--apply 才写库）：
    python scripts/backfill_article_bodies.py --limit 5            # 先试 5 条
    python scripts/backfill_article_bodies.py --apply              # 全量
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.database import build_engine, sessionmaker  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import news_sources  # noqa: E402

MIN_BODY = 200      # 低于此长度视为「正文退化成摘要」
MIN_GAIN = 40       # 新正文至少要比旧的长这么多才值得替换（避免抖动）
DETAILS_MAX = 30


def _needs_body(article: Article) -> bool:
    return len((article.body or "").strip()) < MIN_BODY


def _fetch_html(url: str, timeout: float = 20.0) -> str:
    """复用旧导入器的抓取实现（浏览器 UA），但**不重试、超时短**。

    回填是批量作业：源站被墙 / 503 时重试只会把整轮拖到几十分钟
    （2026-10-22 首次试跑踩过），单条失败就记下来、下一轮再说。
    """
    from scripts import import_articles as legacy

    return legacy.fetch_page(url, timeout=timeout, retries=0)


def run(
    database_url: str,
    *,
    apply: bool,
    limit: int = 0,
    sleep: float = 1.0,
    source: str = "",
) -> dict:
    engine = build_engine(database_url, busy_timeout=60.0)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    stats: dict = {"total": 0, "candidates": 0, "tried": 0, "changed": 0, "failed": 0, "details": []}
    db = factory()
    try:
        rows = list(db.scalars(select(Article).order_by(Article.id)))
        stats["total"] = len(rows)
        for article in rows:
            # --source：按来源名（Article.source，精确匹配）分批回填。源站可达性差别很大
            # （ANIPLEX 常 503、4Gamer 有时 SSL 握手超时），分批才能「能补的先补上」。
            if source and (article.source or "") != source:
                continue
            if not _needs_body(article) or not (article.original_url or "").strip():
                continue
            stats["candidates"] += 1
            # --limit 限制的是**尝试条数**（抓取次数），不是「补到几条」：
            # 被源站拒绝 / 本来就短的文章不算成功，按成功数截断会一路扫完所有候选
            # （2026-10-22 首次试跑就踩了：98 条候选 × 30 秒超时 = 跑到天荒地老）。
            if limit and stats["tried"] >= limit:
                stats["truncated"] = True
                break
            stats["tried"] += 1
            if sleep:
                time.sleep(sleep)  # 礼貌间隔：源站不欢迎连发
            try:
                html = _fetch_html(article.original_url)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                stats["failed"] += 1
                stats["details"].append(
                    {"id": article.id, "status": "failed", "reason": str(exc)[:120]}
                )
                continue
            new_body = news_sources.extract_body(html)
            old_len = len((article.body or "").strip())
            if len(new_body) < old_len + MIN_GAIN:
                stats["details"].append(
                    {
                        "id": article.id,
                        "status": "skip",
                        "reason": f"提取 {len(new_body)} 字，无明显增益（原 {old_len} 字）",
                    }
                )
                continue
            stats["changed"] += 1
            stats["details"].append(
                {
                    "id": article.id,
                    "status": "updated",
                    "reason": f"正文 {old_len}→{len(new_body)} 字｜{article.title[:40]}",
                }
            )
            if apply:
                article.body = new_body[: news_sources.BODY_MAX_CHARS]
        if apply:
            db.commit()
    finally:
        db.close()
        engine.dispose()
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填资讯正文（默认预览，--apply 才写库）")
    parser.add_argument("--apply", action="store_true", help="正式写库")
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="最多补 N 条（0=全量，先小批量试跑用）",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0, metavar="SEC",
        help="每次抓取之间的间隔秒数（默认 1.0，礼貌抓取）",
    )
    parser.add_argument(
        "--source", default="", metavar="NAME",
        help="只处理指定来源（Article.source 精确匹配，如 「ACGCH」/「4Gamer」）",
    )
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args(argv)
    stats = run(
        args.database_url,
        apply=args.apply,
        limit=args.limit,
        sleep=args.sleep,
        source=args.source,
    )
    mode = "已写入" if args.apply else "预览（未写入）"
    scope = f"来源「{args.source}」" if args.source else "全部来源"
    print(
        f"{mode}（{scope}）：共 {stats['total']} 条资讯，正文偏短 {stats['candidates']} 条，"
        f"本轮抓取 {stats['tried']} 条、补到 {stats['changed']} 条、失败 {stats['failed']} 条"
        + ("（已达 --limit 上限）" if stats.get("truncated") else "")
    )
    for item in stats["details"][:DETAILS_MAX]:
        print(f"  [{item['status']}] #{item['id']} {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
