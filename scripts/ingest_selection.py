"""服务器侧：把选品 JSON 清单（select_new_works.py 产出）批量入库。

与 import_bangumi CLI 的区别：CLI 按 Bangumi ID 逐个抓 API（服务器出境不可达，
用不了）；本脚本直接消费**已经构造好的 payload 清单**，逐条走
``_map_and_store(db, payload, apply=True, cover_downloader=False)``——
与 ingest API 同一套映射/查重/入库逻辑，但不需要 Key + 站长 Cookie。

特性：
- 默认 preview（不写库），加 ``--apply`` 才正式入库；
- ``--limit N`` / ``--offset N``：先小批试跑、断点续跑（单批失败不中断，
  重跑时 duplicate 自动跳过，可安全整单重放）；
- 封面不在本步骤下载（cover_downloader=False），入库后统一走
  ``scripts/fetch_covers.py --apply --scope all`` 回填（见 memory/ingest.md）；
- 单 session 跑全程，_map_and_store 内部自行 commit / 失败回滚。

用法（**cwd 必须是 /opt/acgn-test-platform**，相对 DB 路径陷阱见 memory/ingest.md）：
    # 预览前 20 条会怎么落库
    .venv/bin/python3 scripts/ingest_selection.py upload/selection_new.json --limit 20
    # 正式入库（建议先 --limit 50 试跑再全量）
    .venv/bin/python3 scripts/ingest_selection.py upload/selection_new.json --apply --limit 50
    .venv/bin/python3 scripts/ingest_selection.py upload/selection_new.json --apply
    # 断点续跑：前 3000 条已入
    .venv/binpython3 scripts/ingest_selection.py upload/selection_new.json --apply --offset 3000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    SubjectOutcome,
    _map_and_store,
    build_engine,
)


def run_file(
    path: Path,
    *,
    apply: bool,
    limit: int | None,
    offset: int,
    database_url: str | None,
    progress_every: int = 100,
) -> tuple[dict[str, int], list[SubjectOutcome]]:
    """逐条入库，返回 (计数, 全部 outcome)。"""
    payloads = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payloads, list):
        raise SystemExit(f"清单格式不对：{path} 不是 JSON 数组")
    selected = payloads[offset:]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise SystemExit(f"清单为空或 offset={offset} 已越界（共 {len(payloads)} 条）")

    url = database_url or settings.database_url
    print(f"数据库：{url}")
    print(f"清单：{path}（共 {len(payloads)} 条，本次处理 {len(selected)} 条）")
    print(f"模式：{'正式入库（--apply）' if apply else '预览（不写库）'}；封面：本步骤不下载")

    engine = build_engine(url)
    # 无 Alembic，幂等补列：老库缺列时导入仍要能跑
    crud.ensure_schedule_link_columns(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    outcomes: list[SubjectOutcome] = []
    db = session_factory()
    try:
        for index, payload in enumerate(selected, start=1):
            if not isinstance(payload, dict):
                outcomes.append(
                    SubjectOutcome(bangumi_id=-1, status="failed", reason="payload 不是对象")
                )
                continue
            outcomes.append(
                _map_and_store(db, payload, apply=apply, cover_downloader=False)
            )
            if index % progress_every == 0:
                counts: dict[str, int] = {}
                for o in outcomes:
                    counts[o.status] = counts.get(o.status, 0) + 1
                print(f"  进度 {index}/{len(selected)}：{counts}", flush=True)
    finally:
        db.close()
        engine.dispose()

    counts = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    return counts, outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把选品 JSON 清单批量入库（服务器侧）")
    parser.add_argument("selection", help="选品 JSON 文件路径")
    parser.add_argument("--apply", action="store_true", help="正式写库（默认只预览）")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条（试跑用）")
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 条（断点续跑）")
    parser.add_argument("--database-url", default=None, help="默认读应用配置 DATABASE_URL")
    parser.add_argument("--show-failed", type=int, default=20, help="打印失败/跳过明细条数")
    args = parser.parse_args(argv)

    path = Path(args.selection)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2

    counts, outcomes = run_file(
        path,
        apply=args.apply,
        limit=args.limit,
        offset=args.offset,
        database_url=args.database_url,
    )
    print(f"完成：{counts}")

    shown = 0
    for outcome in outcomes:
        if outcome.status in ("failed", "skipped") and shown < args.show_failed:
            print(f"  {outcome.status} #{outcome.bangumi_id}: {outcome.reason}")
            shown += 1
    bad = counts.get("failed", 0)
    print("（failed 可按 ID 重跑或修数据后重放，duplicate 重跑自动跳过）" if bad else "无 failed。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
