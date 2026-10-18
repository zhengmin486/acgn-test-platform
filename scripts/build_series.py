"""一次性脚本：按标题前缀聚类建立系列关联（Series 行 + contents.series_id）。

设计要点：
- 用 crud.series_title_key 归一标题（剥「第X季 / 2期 / season N / 剧场版 / OVA / 特典」等）；
- 前缀合并：series_title_key 不剥「2丁目 / 夏日时光」这类卷/季标记，所以同家族各季会算出
  不同键。这里再做一次前缀聚类（与首页推荐去重同一套语义）：短键是长键的前缀（且短键 >=4 字）
  就并入同一系列，系列名取最短键（基础标题）；
- key 长度 >= 3 且同组合并后 >= 2 部才建系列；
- 幂等：同名 Series 复用（get_or_create_series 按 name 查重），重复跑不产生重复系列行；
  重新跑会把碎片系列的成员改挂到合并后的规范系列，原碎片系列变空（浏览器 inner join 自然不显示）；
- 每个系列处理完 commit 一次，单个系列出错不污染其它系列；
- 默认正式写库；--dry-run 只打印待建系列名与数量，不改库。

用法：
    python scripts/build_series.py --dry-run
    python scripts/build_series.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app import crud  # noqa: E402
from app.models import Content  # noqa: E402


def _merge_groups(groups: dict[str, list[Content]]) -> dict[str, list[Content]]:
    """前缀合并：短键是长键前缀（短键 >=4 字）则并入短键一组。"""
    merged: dict[str, list[Content]] = {}
    for key in sorted(groups.keys(), key=len):
        target = None
        for canon in merged:
            if key.startswith(canon) and len(canon) >= 4:
                target = canon
                break
        if target:
            merged[target].extend(groups[key])
        else:
            merged[key] = list(groups[key])
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description="按标题前缀聚类建立系列关联")
    ap.add_argument("--dry-run", action="store_true", help="只打印待建系列，不写库")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        contents = db.scalars(select(Content)).all()
        raw: dict[str, list[Content]] = {}
        for c in contents:
            key = crud.series_title_key(c.title)
            if len(key) < 3:
                continue
            raw.setdefault(key, []).append(c)
        # 只有本身就 >=2 部的同前缀组才视为系列；单部作品不参与前缀合并，
        # 避免把两个恰巧前缀相同的独立作品误连成系列。
        raw = {k: v for k, v in raw.items() if len(v) >= 2}
        merged = _merge_groups(raw)
        multi = merged

        created = 0
        linked = 0
        for key, members in multi.items():
            if args.dry_run:
                print(f"DRY {key!r} members={len(members)}")
                continue
            series = crud.get_or_create_series(db, key)
            created += 1
            for c in members:
                c.series_id = series.id
                linked += 1
            db.commit()
        print(f"SERIES_CREATED={created} CONTENTS_LINKED={linked} GROUPS={len(multi)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
