"""回填作品的「评分人数」（contents.rating_count）。

为什么需要它
------------
Bangumi 的 ``rating.score`` 是**评分的原始均值**，并不自带「样本够不够」的判断。
所以一部只有 1 个人打分的冷门作品，如果这唯一的人打了 10 分，API 会如实返回
``score = 10.0``；而 Bangumi 站点前端对这类低票条目是显示「--」的、``rank`` 也是 0。

我们过去只看 ``score > 0`` 就入库展示，于是作品库里出现了一批「10 分作品」
（2026-10-27 实测线上 105 部，全部是 anime，抽样 ``total`` 均为 1）。

补这一列之后，展示端就能按人数判断分数能不能显示（口径见 app/rating.py）。

注意：**本脚本不改动 rating 本身**。10.0 是源站如实给的值，保留它才是数据真实，
要修的是「不该把这个分数当回事」，而不是「把分数改成别的数」。

填空语义
--------
``rating_count`` 取不到 / 查不到时留 ``None``（未知），**不要写 0** ——
未知和「确实没人打分」是两回事，写 0 会让展示端误判成低票。

用法
----
    # 先只处理那批 10 分的（快，几十秒）
    python scripts/backfill_rating_count.py --only-suspect          # 预览
    python scripts/backfill_rating_count.py --only-suspect --apply  # 写入

    # 再慢慢把整库补齐（一次别太多，限速跑）
    python scripts/backfill_rating_count.py --apply --limit 500 --sleep 0.3

    # 已有值的行默认跳过；确实要重刷加 --force
    python scripts/backfill_rating_count.py --apply --force --limit 100

线上执行前记得先 export BANGUMI_API_BASE=https://bgmapi.anibt.net
（官方 api.bgm.tv 在服务器上不可达），并且 cd 到项目目录 —— 线上
DATABASE_URL 是相对路径。
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
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402
from app.rating import MIN_RATING_COUNT  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    BANGUMI_API_BASE,
    DEFAULT_USER_AGENT,
    _make_fetcher,
    build_opener,
    resolve_proxy,
)


@dataclass
class Summary:
    scanned: int = 0
    filled: int = 0
    low_confidence: int = 0
    no_rating_data: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回填作品评分人数（contents.rating_count）")
    parser.add_argument("--apply", action="store_true", help="真正写库；不传则只预览")
    parser.add_argument("--limit", type=int, default=50, help="本次最多处理多少条（默认 50）")
    parser.add_argument(
        "--sleep", type=float, default=0.3, help="每次请求间隔秒数，限速用（默认 0.3）"
    )
    parser.add_argument(
        "--only-suspect",
        action="store_true",
        help="只处理评分满分的那批（web 上显示的孤儿 10 分作品），先解燃眉之急",
    )
    parser.add_argument(
        "--min-rating",
        type=float,
        default=None,
        help="只处理评分 >= 该值的作品。高分段最先被排序/推荐顶到前面，"
        "补数优先从这里开始（例如 --min-rating 8.5）；不传则不限分数",
    )
    parser.add_argument(
        "--force", action="store_true", help="连已有 rating_count 的行一起重刷"
    )
    parser.add_argument("--database-url", help=f"覆盖默认数据库（{settings.database_url}）")
    parser.add_argument("--proxy", help="HTTP(S) 代理；不传则直连")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    return parser


def _pick_targets(
    session: Session,
    *,
    limit: int,
    only_suspect: bool,
    force: bool,
    min_rating: float | None = None,
):
    stmt = select(Content.id, Content.bangumi_id, Content.title, Content.rating).where(
        Content.bangumi_id.is_not(None)
    )
    if not force:
        # 只挑「还没问过人数」的；查不到却写 0 会破坏未知语义，所以失败的行也保持 NULL 重挑
        stmt = stmt.where(Content.rating_count.is_(None))
    if only_suspect:
        stmt = stmt.where(Content.rating >= 10.0)
    # 高分段优先：只盯着满分是不够的 —— 9.5 这种只要 2 个人打分也能排到最前
    # （2026-10-28 实测：冒险兄弟/米菲/Незнайка 全是 2 人打出的 9.5）。
    if min_rating is not None:
        stmt = stmt.where(Content.rating >= min_rating)
    return session.execute(stmt.order_by(Content.id).limit(limit)).all()


def _extract_total(payload: dict) -> int | None:
    """从 Bangumi 条目 JSON 里取评分人数。取不到返回 None（未知，不编造）。"""
    raw = (payload.get("rating") or {}).get("total")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or settings.database_url

    print(f"数据库：{database_url}")
    print(f"模式：{'写入（--apply）' if args.apply else '预览（不写库）'}")
    print(f"数据源：{BANGUMI_API_BASE}")
    scope = f"仅 10 分可疑项" if args.only_suspect else "全部缺人数条目"
    if args.min_rating is not None:
        scope = f"评分 >= {args.min_rating} 的缺人数条目"
    print(
        f"范围：{scope}"
        f"{'（含已填的，--force）' if args.force else ''}，上限 {args.limit} 条"
    )
    print(f"参考阈值：评分人数 < {MIN_RATING_COUNT} 视为不足采信（展示为「--」）")
    print()

    engine = build_engine(database_url)
    # 老库补列：没有这一列的库先 ALTER，否则下面的查询直接报错
    added = crud.ensure_content_rating_count_column(engine)
    if added:
        print(f"已为历史库补列：{added}")

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    fetch = _make_fetcher(
        opener=build_opener(resolve_proxy(args.proxy)),
        timeout=args.timeout,
        retries=args.retries,
        user_agent=DEFAULT_USER_AGENT,
        base_url=BANGUMI_API_BASE,
        sleep=time.sleep,
    )

    summary = Summary()
    session = session_factory()
    try:
        targets = _pick_targets(
            session,
            limit=args.limit,
            only_suspect=args.only_suspect,
            force=args.force,
            min_rating=args.min_rating,
        )
        print(f"待处理：{len(targets)} 条\n")
        for index, (content_id, bangumi_id, title, rating) in enumerate(targets, 1):
            summary.scanned += 1
            try:
                total = _extract_total(fetch(bangumi_id))
            except Exception as exc:  # 单条失败不中断整体
                summary.failed += 1
                summary.errors.append(f"#{content_id} bgm={bangumi_id} {title[:20]}：{exc}")
                print(f"  [{index}] ✗ {title[:24]}（{exc}）")
                time.sleep(args.sleep)
                continue

            if total is None:
                summary.no_rating_data += 1
                print(f"  [{index}] - {title[:24]}：没取到人数，留空（未知）")
                time.sleep(args.sleep)
                continue

            if total < MIN_RATING_COUNT:
                summary.low_confidence += 1
                flag = "→ 不足采信，前端显示「--」"
            else:
                flag = ""
            print(f"  [{index}] ✓ {title[:24]}：{total} 人 {flag}")

            if args.apply:
                content = session.get(Content, content_id)
                if content is not None:
                    content.rating_count = total
                    session.commit()
            summary.filled += 1
            time.sleep(args.sleep)
    finally:
        session.close()

    print()
    print("=" * 56)
    verb = "已写入" if args.apply else "预览未落库"
    print(f"扫描 {summary.scanned} 条")
    print(f"  取到评分人数：{summary.filled}（{verb}）")
    print(f"  其中人数不足 (<{MIN_RATING_COUNT})：{summary.low_confidence}")
    print(f"  没取到人数（留 NULL，未知）：{summary.no_rating_data}")
    print(f"  失败：{summary.failed}")
    if summary.errors:
        print("\n失败明细（前 10 条）：")
        for line in summary.errors[:10]:
            print(f"  {line}")
    print("=" * 56)
    if not args.apply and summary.scanned:
        print("\n这是预览。确认无误后加 --apply 写入。")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
