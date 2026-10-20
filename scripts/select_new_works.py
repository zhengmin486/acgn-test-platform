"""从 Bangumi 归档选品，只挑「库里还没有」的作品（净新增）。

背景：线上已有 12012 部（anime 7894 / comic 1428 / game 1464 / novel 1226）。
直接按 rank 选 top-N 会与库里高度重叠、几乎全是 duplicate。本脚本先拿已有
bangumi_id 集合（scripts/fetch_existing_ids.py 产出）排除掉，再在剩余条目里
按 rank 取每类最好的 N 条，保证推上去的是净新增。

选品口径沿用 scripts/select_from_archive.py 的 build_payload / resolve_content_type：
- type=2→anime、4→game；type=1 书籍按 platform 拆 comic(1001)/novel(1002)；
- 跳过 nsfw、rank<=0、score 无效、缺 summary/标题 的条目；
- rank 越小越热门，每类保留 rank 最小的 N 条。

用法：
    python scripts/select_new_works.py \
        --exclude .workbuddy/pilot/existing_ids.json \
        --anime 3000 --comic 1000 --game 1000 --novel 1000 \
        --out .workbuddy/pilot/selection_new.json
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_bangumi import resolve_content_type  # noqa: E402
from scripts.select_from_archive import build_payload, iter_subjects  # noqa: E402

DEFAULT_DUMP = PROJECT_ROOT / ".workbuddy/archive/dump.zip"


def select_new(
    dump_path: Path,
    per_type: dict[str, int],
    *,
    exclude: set[int],
    max_rank: int = 200_000,
) -> tuple[list[dict], dict[str, int], int]:
    """在归档里挑每类不在 exclude 里的 per_type[N] 条，优先热门。

    排序口径（保证「先热门、再长尾」）：
    - 有类别排名的（rank>0）：按 rank 升序（越靠前越热门）；
    - 无排名的（rank=0 / 缺）：归入长尾，按评分 score 降序补位。
    这样像动画这类「头部已被库里收得差不多」的类型，能在排名用尽后，
    自动用评分最高的长尾作品凑满配额，而不是卡在数量不足上。

    单遍流式：每个候选即时 build_payload，通过才进该类型的堆，内存恒定为
    各类型堆大小之和，与归档总量无关。
    """
    heaps: dict[str, list[tuple[tuple, dict]]] = {t: [] for t in per_type}
    scanned = 0

    for entry in iter_subjects(dump_path):
        scanned += 1
        if entry.get("nsfw"):
            continue
        bid = entry.get("id")
        if not isinstance(bid, int) or isinstance(bid, bool):
            continue
        if bid in exclude:
            continue
        content_type = resolve_content_type(entry)
        if content_type is None:
            continue
        want = per_type.get(content_type, 0)
        if want <= 0:
            continue
        rank = entry.get("rank")
        ranked = isinstance(rank, int) and not isinstance(rank, bool) and 0 < rank <= max_rank
        payload = build_payload(entry)
        if payload is None:
            continue
        score = float(entry.get("score") or 0)
        # 排序键：排名组(0, rank) 优先于 长尾组(1, -score)
        sort_key = (0, rank) if ranked else (1, -score)
        heap = heaps[content_type]
        heap.append((sort_key, payload))
        heap.sort(key=lambda pair: pair[0])
        del heap[want:]  # 只留最好的 N 条

    results: list[dict] = []
    counts: dict[str, int] = {}
    for content_type, pairs in heaps.items():
        pairs.sort(key=lambda p: (p[0], p[1]["id"]))
        for sort_key, payload in pairs:
            payload = dict(payload)
            payload["_content_type"] = content_type
            payload["_tier_rank"] = sort_key[1] if sort_key[0] == 0 else 0
            payload["_longtail"] = sort_key[0] == 1
            results.append(payload)
        counts[content_type] = len(pairs)
    results.sort(key=lambda p: (p["_content_type"], p["_tier_rank"], -float(p.get("rating", {}).get("score", 0) or 0), p["id"]))
    return results, counts, scanned


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="选品：只挑库里还没有的作品（净新增）")
    parser.add_argument("--dump", default=str(DEFAULT_DUMP))
    parser.add_argument(
        "--exclude",
        required=True,
        help="已有 bangumi_id 集合（scripts/fetch_existing_ids.py 产出 JSON）",
    )
    parser.add_argument("--anime", type=int, default=0)
    parser.add_argument("--comic", type=int, default=0)
    parser.add_argument("--game", type=int, default=0)
    parser.add_argument("--novel", type=int, default=0)
    parser.add_argument("--max-rank", type=int, default=200_000)
    parser.add_argument(
        "--out", default=str(PROJECT_ROOT / ".workbuddy/pilot/selection_new.json")
    )
    args = parser.parse_args(argv)

    dump = Path(args.dump)
    if not dump.is_file():
        print(f"归档不存在：{dump}", file=sys.stderr)
        return 1
    exclude = set(json.loads(Path(args.exclude).read_text(encoding="utf-8")))
    print(f"已有 ID 集合大小：{len(exclude)}")

    per_type = {
        t: n for t, n in (
            ("anime", args.anime), ("comic", args.comic),
            ("game", args.game), ("novel", args.novel),
        ) if n > 0
    }
    print(f"配额（净新增）：{per_type}")

    payloads, counts, scanned = select_new(
        dump, per_type, exclude=exclude, max_rank=args.max_rank
    )
    print(f"扫描 {scanned} 行；各类选中：{counts}；合计 {len(payloads)} 条")

    if len(payloads) < sum(per_type.values()):
        print(
            "⚠️ 选中数量不足配额——归档里该类型可净新增的条目不够，"
            "可放宽 --max-rank 或确认归档是否够新。"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 去掉内部辅助字段再存（ingest 不需要）
    clean = [{k: v for k, v in p.items() if not k.startswith("_")} for p in payloads]
    out.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写出 {out}")

    for p in payloads[:10]:
        print(
            f"  rank={p['_tier_rank']:<6} {p['_content_type']:<6}"
            f" #{p['id']} {p.get('name_cn') or p.get('name','')[:24]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
