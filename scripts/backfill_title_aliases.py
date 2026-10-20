"""回填 contents.title_original / aliases（服务器侧，按官方归档）。

为什么需要（2026-10-19 搜索增强）：
    title 存 name_cn or name 二选一，中文名条目的日文原名与 infobox「别名」
    此前不入库，搜原名/别名搜不到。历史行由本脚本按 Bangumi 官方每周归档
    （/opt/acgn-archive/dump.zip，subject.jsonlines 含 name / name_cn /
    infobox wiki 字符串）流式回填，12251 条一次跑完，零外网。

设计要点：
- **重活丢服务器**：归档和库都在服务器上，本脚本在服务器跑，不占用本机代理；
- **只填不改**：title_original 仅在当前为空时写入、aliases 仅在当前为空数组
  时写入——增量入库（import_bangumi）与人工维护的值一律不覆盖，重跑幂等；
- **共用过滤口径**：别名提取复用 scripts.import_bangumi.extract_alias_names
  （wiki 字符串先经 select_from_archive.parse_wiki_infobox 转 API 结构），
  防止回填与增量两套规则漂移；
- 大批量写库避开封面回填时段（SQLite 共写锁），busy_timeout 60s；
- 默认 dry-run（只报计划变更数），加 ``--apply`` 才真正写库。

用法（服务器）：
    cd /opt/acgn-test-platform
    .venv/bin/python scripts/backfill_title_aliases.py --dry-run   # 或默认
    .venv/bin/python scripts/backfill_title_aliases.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Content  # noqa: E402
from scripts.import_bangumi import extract_alias_names  # noqa: E402
from scripts.select_from_archive import iter_subjects, parse_wiki_infobox  # noqa: E402


def backfill(
    db,
    dump_path: Path,
    *,
    apply: bool = False,
) -> dict:
    """流式扫归档，回填站内 Bangumi 条目的原名与别名，返回统计。

    统计字段：
    - matched         归档里找到且站内需要的条目数
    - updated         实际写入变更的行数（apply 模式）
    - planned         dry-run 下「将会写入」的行数
    - already_set     站内已有值、按「只填不改」跳过的行数
    - no_alias_data   归档条目没有可回填内容（原名与别名都提不出来）
    - not_in_archive  站内有 bangumi_id 但归档里没扫到的条目数（收尾统计）
    """
    rows = db.execute(
        select(Content).where(Content.bangumi_id.is_not(None))
    ).scalars().all()
    pending = {row.bangumi_id: row for row in rows}
    stats = {
        "matched": 0,
        "updated": 0,
        "planned": 0,
        "already_set": 0,
        "no_alias_data": 0,
        "not_in_archive": 0,
    }

    for entry in iter_subjects(dump_path):
        subject_id = entry.get("id")
        if subject_id not in pending:
            continue
        row = pending.pop(subject_id)
        stats["matched"] += 1

        name = (entry.get("name") or "").strip()
        title_original = name if name and name != row.title else None
        payload = {"infobox": parse_wiki_infobox(entry.get("infobox") or "")}
        aliases = extract_alias_names(payload, row.title, title_original)

        want_original = title_original if row.title_original is None else None
        want_aliases = aliases if not row.aliases else None
        if want_original is None and want_aliases is None:
            stats["already_set"] += 1
            continue
        if want_original is None and not want_aliases:
            stats["no_alias_data"] += 1
            continue

        stats["planned"] += 1
        if not apply:
            continue
        if want_original is not None:
            row.title_original = want_original
        if want_aliases is not None:
            row.aliases = want_aliases
        stats["updated"] += 1

    stats["not_in_archive"] = len(pending)
    if apply:
        db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump",
        default="/opt/acgn-archive/dump.zip",
        help="Bangumi 官方归档 zip（默认 /opt/acgn-archive/dump.zip）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 库路径（默认取 settings.database_url 的 sqlite 文件路径）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；缺省 dry-run 只报告计划变更数",
    )
    args = parser.parse_args()

    from app.config import settings

    if args.db:
        db_path = args.db
    else:
        db_url = settings.database_url
        db_path = db_url.removeprefix("sqlite:///") if db_url.startswith("sqlite:///") else db_url
        # 相对路径以项目根为基准（与 config 的 sqlite:///./local_dev.db 语义一致）
        if not Path(db_path).is_absolute():
            db_path = str(PROJECT_ROOT / db_path)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 60},  # 大批量写库避让其他写入者（封面回填等）
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    dump_path = Path(args.dump)
    if not dump_path.exists():
        print(f"归档不存在：{dump_path}")
        return 2
    with factory() as db:
        stats = backfill(db, dump_path, apply=args.apply)
    label = "已写入" if args.apply else "计划写入(dry-run)"
    print(
        f"归档 {dump_path.name} | 匹配 {stats['matched']} | {label} {stats['updated'] if args.apply else stats['planned']}"
        f" | 已有值跳过 {stats['already_set']} | 归档无数据 {stats['no_alias_data']}"
        f" | 归档未覆盖 {stats['not_in_archive']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
