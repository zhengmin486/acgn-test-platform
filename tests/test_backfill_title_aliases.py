"""回填脚本（scripts/backfill_title_aliases.py）的行为测试。

用临时 zip 模拟官方归档（subject.jsonlines，infobox 为 wiki 字符串），
覆盖：原名与别名的回填、wiki 噪音过滤、「只填不改」幂等、dry-run 不写库。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import Base, Content  # noqa: E402
from scripts.backfill_title_aliases import backfill  # noqa: E402


def make_dump(tmp_path: Path, entries: list[dict]) -> Path:
    """造一个 mini dump.zip（成员名与官方归档一致）。"""
    dump = tmp_path / "dump.zip"
    with zipfile.ZipFile(dump, "w") as archive:
        lines = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
        archive.writestr("subject.jsonlines", lines)
    return dump


def entry(subject_id: int, name: str, name_cn: str, infobox: str) -> dict:
    return {
        "id": subject_id,
        "name": name,
        "name_cn": name_cn,
        "infobox": infobox,
        "score": 8.0,
        "summary": "s",
    }


WIKI = "|中文名: 凉宫春日的忧郁\n|别名: SOS团\n|别名: 123\n|别名: EVA\n"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bf.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        session.add_all(
            [
                Content(
                    title="凉宫春日的忧郁",
                    type="anime",
                    summary="s",
                    rating=8.0,
                    bangumi_id=4,
                ),
                # 已有增量入库写入的值：验证「只填不改」
                Content(
                    title="某日常",
                    type="anime",
                    summary="s",
                    rating=7.0,
                    bangumi_id=5,
                    title_original="ある日常",
                    aliases=["手填别名"],
                ),
                # 站内有但归档没有
                Content(
                    title="归档外条目",
                    type="anime",
                    summary="s",
                    rating=6.0,
                    bangumi_id=9,
                ),
            ]
        )
        session.commit()
        yield session
    finally:
        session.close()


def test_backfill_fills_and_filters(db, tmp_path):
    dump = make_dump(tmp_path, [entry(4, "涼宮ハルヒの憂鬱", "凉宫春日的忧郁", WIKI)])
    stats = backfill(db, dump, apply=True)
    db.commit()

    row = db.scalar(select(Content).where(Content.bangumi_id == 4))
    assert row.title_original == "涼宮ハルヒの憂鬱"
    # 「123」纯数字被过滤，SOS团 / EVA 保留
    assert row.aliases == ["SOS团", "EVA"]
    assert stats["matched"] == 1
    assert stats["updated"] == 1
    assert stats["not_in_archive"] == 2  # bangumi_id=5（无归档条目）与 9 归档外


def test_backfill_never_overwrites(db, tmp_path):
    """已有 title_original / aliases 的行一律跳过（人工/增量值优先）。"""
    dump = make_dump(tmp_path, [entry(5, "覆盖尝试", "某日常", "|别名: 覆盖别名\n")])
    stats = backfill(db, dump, apply=True)

    row = db.scalar(select(Content).where(Content.bangumi_id == 5))
    assert row.title_original == "ある日常"
    assert row.aliases == ["手填别名"]
    assert stats["already_set"] == 1
    assert stats["updated"] == 0


def test_backfill_dry_run_writes_nothing(db, tmp_path):
    dump = make_dump(tmp_path, [entry(4, "涼宮ハルヒの憂鬱", "凉宫春日的忧郁", WIKI)])
    stats = backfill(db, dump, apply=False)

    row = db.scalar(select(Content).where(Content.bangumi_id == 4))
    assert row.title_original is None
    assert row.aliases == []
    assert stats["planned"] == 1 and stats["updated"] == 0


def test_backfill_idempotent_rerun(db, tmp_path):
    dump = make_dump(tmp_path, [entry(4, "涼宮ハルヒの憂鬱", "凉宫春日的忧郁", WIKI)])
    backfill(db, dump, apply=True)
    db.commit()
    stats = backfill(db, dump, apply=True)
    assert stats["updated"] == 0 and stats["already_set"] == 1


def test_backfill_no_useful_data(db, tmp_path):
    """归档条目提不出原名（与 title 相同）也没别名 → no_alias_data。"""
    dump = make_dump(tmp_path, [entry(4, "凉宫春日的忧郁", "凉宫春日的忧郁", "")])
    stats = backfill(db, dump, apply=True)
    assert stats["no_alias_data"] == 1
    row = db.scalar(select(Content).where(Content.bangumi_id == 4))
    assert row.title_original is None
