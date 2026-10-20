"""回填作品关系：前作 / 续作 / 衍生作（content_relations）。

详情页「系列作品」区要展示真实的续衍关系，但库里只有 series_id（覆盖约 13%）
且它是「多季 / 剧场版」口径，前作 / 续作 / 衍生作无处可存——故新增
``content_relations``，由本脚本从 Bangumi 关联条目端点灌数据。

**不靠标题前缀推断关系**：前缀聚类（crud.series_title_key）只服务首页分组去重，
拿它当系列证据会把「魔法少女小圆 / 魔法少女奈叶」这类误并。本脚本只认
关联条目端点返回的 relation 字段，并且：

- 只收「同一条作品线」的三种关系（见 crud.BANGUMI_RELATION_MAP：
  续集→续作、前传→前作、衍生 / 总集篇→衍生作）；音乐 / 画集 / 书籍 /
  三次元 / 联动等不是续衍关系，一律不写；
- **只保留双方都在库内**的配对（按 bangumi_id 匹配），不写悬空 id；
- 只写「本作 → 对方」方向，对端由它自己的回填补齐（双向各写一条时
  crud.list_content_relations 会取关系更近的一条）；
- 替换式写入（crud.replace_content_relations），重复运行幂等、不堆叠；
- 默认只预览，``--apply`` 才写库；已有关系的作品默认跳过（``--force`` 重算）。

用法（默认预览）：
    python scripts/backfill_content_relations.py
    python scripts/backfill_content_relations.py --apply --limit 200
    python scripts/backfill_content_relations.py --apply --content-id 7 8 9
    python scripts/backfill_content_relations.py --apply --force

线上（官方 API 不可达，走镜像）：
    BANGUMI_API_BASE=https://bgmapi.anibt.net python scripts/backfill_content_relations.py --apply

退出码：0 正常；1 指定了 --content-id 但一部都没匹配到。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine, sessionmaker  # noqa: E402
from app.entity_links import parse_related_subjects  # noqa: E402
from app.models import Content, ContentRelation  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    BANGUMI_API_BASE,
    build_opener,
    fetch_related_subjects,
    resolve_proxy,
)

# 每次请求之间的间隔（秒）：关联条目端点请求量很大，别把镜像打疼。
DEFAULT_SLEEP = 0.2


def run(
    database_url: str,
    *,
    apply: bool = False,
    limit: int | None = None,
    content_ids: list[int] | None = None,
    force: bool = False,
    proxy: str | None = None,
    sleep_seconds: float = DEFAULT_SLEEP,
) -> dict:
    """回填作品关系，返回统计字典（默认不写库）。"""
    engine = build_engine(database_url, busy_timeout=60.0)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()

    stmt = select(Content.id, Content.bangumi_id).where(
        Content.bangumi_id.is_not(None)
    )
    if content_ids:
        stmt = stmt.where(Content.id.in_([int(value) for value in content_ids]))
    rows = db.execute(stmt).all()
    # 只认双方都在库内的配对：bangumi_id -> 本站 content_id
    id_by_bangumi = {int(bangumi_id): cid for cid, bangumi_id in rows}
    targets = [(cid, int(bangumi_id)) for cid, bangumi_id in rows]
    if limit is not None:
        targets = targets[:limit]

    opener = build_opener(resolve_proxy(proxy))
    stats = {
        "scanned": 0,
        "written": 0,  # 实际写入的关系条数
        "matched": 0,  # 有可用关系的作品数
        "skipped": 0,  # 已有关系且未 --force
        "failed": 0,
    }

    for position, (cid, bangumi_id) in enumerate(targets):
        stats["scanned"] += 1
        if not force:
            existing = db.scalar(
                select(func.count())
                .select_from(ContentRelation)
                .where(ContentRelation.from_content_id == cid)
            )
            if existing:
                stats["skipped"] += 1
                continue
        try:
            payload = fetch_related_subjects(bangumi_id, opener=opener)
        except Exception as exc:  # 网络类异常不中断整体回填
            stats["failed"] += 1
            print(f"[fetch-failed] 作品 {cid} (bgm {bangumi_id}): {exc}", flush=True)
        else:
            pairs: list[tuple[int, str]] = []
            for subject_id, relation, _title in parse_related_subjects(payload):
                relation_type = crud.BANGUMI_RELATION_MAP.get(relation)
                if not relation_type:
                    continue
                other_id = id_by_bangumi.get(int(subject_id))
                if not other_id or other_id == cid:
                    continue
                pairs.append((other_id, relation_type))
            if pairs:
                stats["matched"] += 1
                stats["written"] += len(pairs)
                if apply:
                    crud.replace_content_relations(db, cid, pairs)
        if sleep_seconds and position % 50 == 0:
            time.sleep(sleep_seconds)

    db.close()
    engine.dispose()
    stats["apply"] = apply
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="回填作品关系（前作 / 续作 / 衍生作，默认预览）"
    )
    parser.add_argument("--apply", action="store_true", help="正式写库")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 部")
    parser.add_argument(
        "--content-id", type=int, nargs="+", default=None, help="只处理指定作品"
    )
    parser.add_argument(
        "--force", action="store_true", help="已有关系的作品也重算"
    )
    parser.add_argument("--proxy", default=None, help="HTTP 代理")
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP, help="每 50 部之间的间隔秒数"
    )
    args = parser.parse_args()

    print(f"数据源：{BANGUMI_API_BASE}（关联条目端点）")
    stats = run(
        args.database_url,
        apply=args.apply,
        limit=args.limit,
        content_ids=args.content_id,
        force=args.force,
        proxy=args.proxy,
        sleep_seconds=args.sleep,
    )
    mode = "正式写入" if stats["apply"] else "预览（未写库）"
    print(
        f"{mode}：扫描 {stats['scanned']} 部 / 命中关系 {stats['matched']} 部 / "
        f"关系条数 {stats['written']} / 跳过已有 {stats['skipped']} / 失败 {stats['failed']}"
    )
    if args.content_id and stats["scanned"] == 0:
        print("指定的 --content-id 一部都没匹配到")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
