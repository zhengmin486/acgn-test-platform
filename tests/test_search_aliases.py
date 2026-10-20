"""搜索增强（title_original / aliases）的行为测试。

覆盖：
- extract_alias_names：infobox「别名」提取 + 垃圾值过滤（太短/纯数字/重复）；
- map_subject：日文原名（name ≠ name_cn 时）与别名一并映射；
- _map_and_store：入库后 contents 行带上两列（增量链路）；
- ensure_content_title_alias_columns：老库幂等补列（加列不生效铁律的 ensure_* 模式）。

用临时 SQLite，不触网、不写真实库。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.crud import ensure_content_title_alias_columns  # noqa: E402
from app.models import Base, Content  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    MAX_ALIASES_PER_SUBJECT,
    SubjectMappingError,
    _map_and_store,
    extract_alias_names,
    map_subject,
)


def payload_with_aliases(subject_id: int = 1) -> dict:
    """带「别名」键与中日双名的最小 payload（type=2 动画）。"""
    return {
        "id": subject_id,
        "name": "涼宮ハルヒの憂鬱",
        "name_cn": "凉宫春日的忧郁",
        "type": 2,
        "summary": "简介文字",
        "date": "2006-04-01",
        "rating": {"score": 8.5, "total": 100},
        "tags": [{"name": "校园"}, {"name": "科幻"}],
        "infobox": [
            {"key": "中文名", "value": "凉宫春日的忧郁"},
            {
                "key": "别名",
                "value": [
                    {"v": "SOS团"},
                    {"v": "The Melancholy of Haruhi Suzumiya"},
                    {"v": "第四个"},  # wiki 噪音：≤1 字符级别的垃圾值
                    {"v": "123456"},  # 纯数字
                    {"v": "凉宫春日的忧郁"},  # 与中文名重复
                    {"v": "涼宮ハルヒの憂鬱"},  # 与日文原名重复
                    {"v": "EVA"},  # 长度 3，合法保留
                ],
            },
        ],
    }


class TestExtractAliasNames:
    def test_extracts_filters_and_dedupes(self):
        p = payload_with_aliases()
        aliases = extract_alias_names(p, "凉宫春日的忧郁", "涼宮ハルヒの憂鬱")
        # 「第四个」长度 3 会被保留（规则只丢 ≤1 字符），纯数字与重名丢弃
        assert aliases == ["SOS团", "The Melancholy of Haruhi Suzumiya", "第四个", "EVA"]

    def test_no_infobox_returns_empty(self):
        p = payload_with_aliases()
        p.pop("infobox")
        assert extract_alias_names(p, "凉宫春日的忧郁", None) == []

    def test_infobox_not_list_returns_empty(self):
        p = payload_with_aliases()
        p["infobox"] = "坏数据"
        assert extract_alias_names(p, "凉宫春日的忧郁", None) == []

    def test_string_style_value_supported(self):
        p = payload_with_aliases()
        p["infobox"] = [{"key": "别名", "value": [{"v": "EVA"}, {"v": "新世纪天鹰战士"}]}]
        assert extract_alias_names(p, "新世纪福音战士", None) == ["EVA", "新世纪天鹰战士"]

    def test_single_char_and_pure_digit_dropped(self):
        p = payload_with_aliases()
        p["infobox"] = [{"key": "别名", "value": [{"v": "A"}, {"v": "7"}, {"v": "007"}, {"v": "EVA"}]}]
        # 「A」≤1 字符、「7」「007」纯数字（isdigit）→ 全丢，只留正常别名
        assert extract_alias_names(p, "某作品", None) == ["EVA"]

    def test_cap_limit(self):
        p = payload_with_aliases()
        p["infobox"] = [
            {"key": "别名", "value": [{"v": f"别名{i}号xyz"} for i in range(50)]}
        ]
        aliases = extract_alias_names(p, "某作品", None)
        assert len(aliases) == MAX_ALIASES_PER_SUBJECT


class TestMapSubject:
    def test_title_original_set_when_name_differs(self):
        subject = map_subject(payload_with_aliases())
        assert subject.title == "凉宫春日的忧郁"
        assert subject.title_original == "涼宮ハルヒの憂鬱"
        assert "SOS团" in subject.aliases

    def test_title_original_none_when_same_as_title(self):
        p = payload_with_aliases()
        p["name_cn"] = ""
        subject = map_subject(p)
        assert subject.title == "涼宮ハルヒの憂鬱"
        assert subject.title_original is None


class TestStore:
    @pytest.fixture
    def db(self, tmp_path):
        engine = create_engine(
            f"sqlite:///{tmp_path / 'store.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def test_map_and_store_writes_columns(self, db):
        outcome = _map_and_store(db, payload_with_aliases(7), apply=True, cover_downloader=None)
        assert outcome.status == "imported"
        row = db.scalar(select(Content).where(Content.bangumi_id == 7))
        assert row is not None
        assert row.title_original == "涼宮ハルヒの憂鬱"
        assert "SOS团" in row.aliases

    def test_duplicate_keeps_original_row(self, db):
        assert _map_and_store(db, payload_with_aliases(8), apply=True, cover_downloader=None).status == "imported"
        outcome = _map_and_store(db, payload_with_aliases(8), apply=True, cover_downloader=None)
        assert outcome.status == "duplicate"


class TestEnsureColumns:
    def test_adds_columns_to_legacy_table(self, tmp_path):
        """老库（无两列）补列成功，且二次调用幂等返回空。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        # 先手工造一张「历史形态」的 contents 表（不含新列）
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE contents ("
                "id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,"
                "type VARCHAR(20) NOT NULL, summary TEXT NOT NULL, rating FLOAT NOT NULL)"
            )
            conn.exec_driver_sql(
                "INSERT INTO contents (title, type, summary, rating)"
                " VALUES ('旧条目', 'anime', '简介', 7.0)"
            )
        added = ensure_content_title_alias_columns(engine)
        assert added == ["title_original", "aliases"]
        # 老行可见且 aliases 默认空数组
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT title_original, aliases FROM contents WHERE title='旧条目'"
            ).fetchone()
        assert row[0] is None
        assert row[1] in ("[]", None)
        # 幂等：第二次不再加列
        assert ensure_content_title_alias_columns(engine) == []

    def test_fresh_table_via_metadata(self, tmp_path):
        """create_all 建的新表自带两列，ensure 不重复加。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
        Base.metadata.create_all(engine)
        assert ensure_content_title_alias_columns(engine) == []


def test_mapping_error_still_raised_without_titles():
    p = payload_with_aliases()
    p["name"] = ""
    p["name_cn"] = ""
    with pytest.raises(SubjectMappingError):
        map_subject(p)
