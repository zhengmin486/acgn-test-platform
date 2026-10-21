"""游民星空存量清理 dry-run 清单（只读，不删除任何数据）。

口径（2026-10-21 用户定稿）：只保留「新作 / 改编 / 动画化 / 制作确定 / 正式 PV」
等作品情报；剧情解读、盘点水文、单件手办播报**建议删除**——建议≠执行，
实际删除必须由人工逐条确认后另行处理（本脚本没有任何删除路径）。

判定是词面启发式，产出供人工过目：命中保留词 → 建议保留；命中删除词且无
保留词 → 建议删除；两者都不命中 → 默认保留（宁留勿删）。

用法：
    python scripts/gamersky_dryrun.py                  # 打印清单
    python scripts/gamersky_dryrun.py --out 清单.md    # 写 markdown 文件
    python scripts/gamersky_dryrun.py --database-url … # 指定库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402

GAMERSKY_LABEL = "游民星空 ACG"

# 保留信号：作品情报（新作 / 改编 / 制作 / 放送 / 完结 / 声优 / 官宣）。
KEEP_WORDS: tuple[str, ...] = (
    "动画化", "漫画化", "游戏化", "改编", "新作", "制作确定", "制作决定",
    "制作公开", "PV", "预告", "视觉图", "主视觉", "先导", "正片", "本篇",
    "完结", "最终话", "放送", "播出", "开播", "定档", "上映", "配信",
    "连载", "新连载", "续作", "续篇", "重制", "重开", "声优", "Cast",
    "公开", "官宣", "确定", "决定", "发表", "发售日", "发售决定", "开订",
    "第",  # 「第X话 / 第X卷 / 第X季」类情报
)

# 删除信号：剧情解读 / 盘点水文 / 二创还原 / 单件商品播报。
DROP_WORDS: tuple[str, ...] = (
    "解读", "揭晓", "曝光", "候选", "盘点", "排名", "排行", "预测",
    "评选", "人气投票", " strongest", "最强的是", "凌驾", "实力",
    "网友", "梗图", "还原", "吐槽", "整活", "二创",
    "手办", "一番赏", "景品", "马克杯", "挂画", "色纸",
)


def classify_title(title: str) -> tuple[str, str]:
    """返回 (建议, 命中词)；建议取值 保留 / 删除建议。"""
    hits_keep = [w for w in KEEP_WORDS if w.lower() in title.lower()]
    hits_drop = [w for w in DROP_WORDS if w.lower() in title.lower()]
    if hits_drop and not hits_keep:
        return "删除建议", "、".join(hits_drop[:3])
    return "保留", "、".join(hits_keep[:3]) if hits_keep else "（无明确信号，默认保留）"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="游民星空存量清理 dry-run 清单（只读）")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--out", default="", help="写入 markdown 文件（默认只打印）")
    args = parser.parse_args(argv)

    engine = build_engine(args.database_url, busy_timeout=60.0)
    rows = []
    from sqlalchemy.orm import Session

    with Session(bind=engine) as db:
        articles = db.scalars(
            select(Article)
            .where(Article.source == GAMERSKY_LABEL)
            .order_by(Article.id.desc())
        ).all()
        for article in articles:
            verdict, hit = classify_title(article.title)
            rows.append((article.id, article.title, verdict, hit))
    engine.dispose()

    drop_count = sum(1 for r in rows if r[2] == "删除建议")
    lines = [
        f"# 游民星空存量清理 dry-run 清单（共 {len(rows)} 条，建议删除 {drop_count} 条）",
        "",
        "> 本清单只是建议，未删除任何数据；逐条人工确认后另行处理。",
        "",
    ]
    for article_id, title, verdict, hit in rows:
        lines.append(f"- [{verdict}] #{article_id} {title}（命中：{hit}）")
    text = "\n".join(lines)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写出 {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
