"""作品关系（同系列 / 同作者 / 同公司）功能测试。

覆盖：
- 识别解析（app/entity_links.py）：infobox 白名单、HTML 多值拆分、
  系列键、关联条目端点防御性解析、标题公共前缀；
- 数据层（app/crud.py）：entities / content_entity_links / series 读写、
  替换式语义、索引与反查、series_id 幂等补列、creator 搜索筛选；
- 导入集成（scripts/import_bangumi.py）：随作品识别写入、无可用键时静默跳过；
- 回填 CLI（scripts/link_entities.py）：预览 / apply / force / content_ids /
  series 四种形态与退出码；
- 页面：/creators 索引与详情、/contents?creator= 筛选、详情页关系 chips
  与系列作品卡、/manage/entities 手工补删（权限 + PRG 回显）。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离；
CLI 与网络层全部用注入的假数据，不访问真实网络。
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import crud, entity_links
from app.database import Base, get_db
from app.main import app
from app.models import Content, ContentEntityLink, Entity, Series
from auth_helpers import create_accounts, login_as_admin, login_as_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "link_entities.py"

# scripts/ 不是包，按文件路径加载被测 CLI 模块（与直接运行脚本等价）。
_spec = importlib.util.spec_from_file_location("link_entities_for_tests", SCRIPT_PATH)
link_entities = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = link_entities
_spec.loader.exec_module(link_entities)


# ---------- 夹具与工具 ----------


def _infobox_payload(subject_id: int = 100, infobox=None) -> dict:
    """拼一个最小 Bangumi 条目 JSON（只带识别需要的字段）。"""
    return {
        "id": subject_id,
        "name_cn": "测试作品",
        "name": "Test Work",
        "type": 2,
        "summary": "简介",
        "rating": {"score": 7.5},
        "infobox": infobox if infobox is not None else [],
    }


@pytest.fixture
def factory(tmp_path):
    """独立临时库：三部作品（其中两部互为同系列），不播种。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'entities.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        db.add_all(
            [
                Content(
                    title="为美好的世界献上祝福 第二季",
                    type="anime",
                    summary="异世界喜剧第二季。",
                    rating=8.5,
                    bangumi_id=100,
                ),
                Content(
                    title="为美好的世界献上祝福 第一季",
                    type="anime",
                    summary="异世界喜剧第一季。",
                    rating=7.5,
                    bangumi_id=99,
                ),
                Content(
                    title="孤独的作品",
                    type="comic",
                    summary="没有关系的作品。",
                    rating=6.0,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def page_env(factory):
    """页面用例环境：TestClient + get_db 覆盖 + 管理员/普通用户客户端。"""
    create_accounts(factory, admin=True, user=True)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            session_factory=factory,
            client=TestClient(app, follow_redirects=False),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _recognized_factory(factory):
    """在库里写入一套识别结果：原作/动画制作 + 同一系列，返回作品 id。"""
    db = factory()
    try:
        crud.replace_content_entity_links(
            db, 1, [("晓枣", crud.ENTITY_TYPE_PERSON, "原作"), ("动画工房", crud.ENTITY_TYPE_COMPANY, "动画制作")]
        )
        crud.set_content_series(db, 1, "为美好的世界献上祝福")
        crud.set_content_series(db, 2, "为美好的世界献上祝福")
    finally:
        db.close()
    return factory


# ---------- 1. 识别解析（纯函数） ----------


class TestParseInfobox:
    def test_parse_normal_pairs(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "作者", "value": "晓枣"},
                {"key": "动画制作", "value": "动画工房"},
            ]
        )
        assert entity_links.parse_infobox(payload) == [("作者", "晓枣"), ("动画制作", "动画工房")]

    def test_parse_skips_bad_shapes(self):
        payload = _infobox_payload(
            infobox=[
                "不是字典",
                {"key": "", "value": "空键"},
                {"key": "价格", "value": ""},
                {"key": None, "value": "空键名"},
                {"key": "页数", "value": 123},
                {"key": "作者", "value": "保留"},
            ]
        )
        assert entity_links.parse_infobox(payload) == [("作者", "保留")]

    def test_parse_non_list_returns_empty(self):
        assert entity_links.parse_infobox({"infobox": "作者"}) == []
        assert entity_links.parse_infobox({}) == []
        assert entity_links.parse_infobox(None) == []


class TestNormalizeKey:
    def test_casefold_and_whitespace(self):
        assert entity_links.normalize_infobox_key(" Series ") == "series"
        assert entity_links.normalize_infobox_key("动画 制作") == "动画制作"
        assert entity_links.normalize_infobox_key("Ａｎｉｍｅ") == "anime"
        assert entity_links.normalize_infobox_key("") == ""


class TestSplitValue:
    def test_html_tags_and_separators(self):
        value = '<a href="/p/1">Type-Moon</a><br>奈须蘑菇、武内崇'
        assert entity_links.split_infobox_value(value) == ["Type-Moon", "奈须蘑菇", "武内崇"]

    def test_entities_and_whitespace(self):
        assert entity_links.split_infobox_value("A &amp; B<br>　C　") == ["A & B", "C"]

    def test_non_string_and_empty(self):
        assert entity_links.split_infobox_value(None) == []
        assert entity_links.split_infobox_value(123) == []
        assert entity_links.split_infobox_value("   ") == []


class TestRecognize:
    def test_whitelist_keys_and_types(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "原作", "value": "<a>Type-Moon</a>"},
                {"key": "音乐", "value": "梶浦由记"},
                {"key": "アニメーション制作", "value": "ufotable"},
                {"key": "発行元", "value": "角川书店"},
            ]
        )
        recognition = entity_links.recognize(7, payload)
        assert recognition.content_id == 7
        assert recognition.links == [
            entity_links.EntityLink("Type-Moon", crud.ENTITY_TYPE_PERSON, "原作"),
            entity_links.EntityLink("梶浦由记", crud.ENTITY_TYPE_PERSON, "音乐"),
            entity_links.EntityLink("ufotable", crud.ENTITY_TYPE_COMPANY, "动画制作"),
            entity_links.EntityLink("角川书店", crud.ENTITY_TYPE_COMPANY, "出版社"),
        ]

    def test_metadata_keys_ignored(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "价格", "value": "¥ 680"},
                {"key": "发售日", "value": "2004年1月30日"},
                {"key": "页数", "value": "300"},
                {"key": "丛书", "value": "电击文库"},
            ]
        )
        recognition = entity_links.recognize(1, payload)
        assert recognition.links == []
        assert recognition.series is None

    def test_series_key(self):
        payload = _infobox_payload(infobox=[{"key": "系列", "value": "某系列"}])
        recognition = entity_links.recognize(1, payload)
        assert recognition.series == "某系列"
        assert recognition.links == []

    def test_dedupe_same_name_and_role(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "作者", "value": "晓枣"},
                {"key": "著者", "value": "晓枣"},
            ]
        )
        recognition = entity_links.recognize(1, payload)
        assert [(link.name, link.role) for link in recognition.links] == [("晓枣", "原作")]

    def test_same_name_different_roles_both_kept(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "原作", "value": "奈须蘑菇"},
                {"key": "系列构成", "value": "奈须蘑菇"},
            ]
        )
        recognition = entity_links.recognize(1, payload)
        assert [(link.name, link.role) for link in recognition.links] == [
            ("奈须蘑菇", "原作"),
            ("奈须蘑菇", "系列构成"),
        ]

    def test_truncated_to_max_links(self):
        many = [{"key": "作者", "value": f"作者{i}"} for i in range(30)]
        recognition = entity_links.recognize(1, _infobox_payload(infobox=many))
        assert len(recognition.links) == crud.MAX_CONTENT_ENTITY_LINKS
        assert recognition.links[0].name == "作者0"
        assert recognition.links[-1].name == f"作者{crud.MAX_CONTENT_ENTITY_LINKS - 1}"

    def test_truncation_still_scans_series_key(self):
        many = [{"key": "作者", "value": f"作者{i}"} for i in range(30)]
        many.append({"key": "系列", "value": "后面的系列"})
        recognition = entity_links.recognize(1, _infobox_payload(infobox=many))
        assert len(recognition.links) == crud.MAX_CONTENT_ENTITY_LINKS
        assert recognition.series == "后面的系列"

    def test_empty_infobox(self):
        recognition = entity_links.recognize(1, _infobox_payload())
        assert not recognition.has_data()
        assert recognition.describe() == ""

    def test_describe(self):
        payload = _infobox_payload(
            infobox=[
                {"key": "原作", "value": "晓枣"},
                {"key": "动画制作", "value": "动画工房"},
                {"key": "系列", "value": "某系列"},
            ]
        )
        recognition = entity_links.recognize(1, payload)
        assert recognition.describe() == "作者/公司 2 项，系列『某系列』"


class TestSeriesRelation:
    def test_is_series_relation(self):
        assert entity_links.is_series_relation("续集")
        assert entity_links.is_series_relation(" sequel ")
        assert not entity_links.is_series_relation("不同演绎")
        assert not entity_links.is_series_relation(None)

    def test_parse_related_subjects(self):
        payload = [
            {"id": 99, "relation": "续集", "name_cn": "前作"},
            {"id": 98, "relation": "外传", "name": "外传作品"},
            {"id": "坏", "relation": "续集", "name_cn": "x"},
            {"relation": "续集", "name_cn": "没有 id"},
            "不是字典",
        ]
        assert entity_links.parse_related_subjects(payload) == [
            (99, "续集", "前作"),
            (98, "外传", "外传作品"),
        ]
        assert entity_links.parse_related_subjects({"not": "a list"}) == []

    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("为美好的世界献上祝福 第二季", "为美好的世界献上祝福 第一季", "为美好的世界献上祝福"),
            ("某科学的超电磁炮", "某科学的超电磁炮S", "某科学的超电磁炮"),
            ("命运石之门", "命运石之门 负荷领域的既视感", "命运石之门"),
            ("新甲", "新乙", None),
            ("某", "某2", None),
            ("同一标题", "同一标题", None),
            ("", "某2", None),
        ],
    )
    def test_common_series_name(self, a, b, expected):
        assert entity_links.common_series_name(a, b) == expected


# ---------- 2. 数据层 ----------


class TestEntityCrud:
    def test_normalize_entity_name(self):
        assert crud.normalize_entity_name("  Ａ-１ Pictures  ") == "A-1 Pictures"
        assert crud.normalize_entity_name(None) == ""
        assert crud.normalize_entity_name("Type-Moon") == "Type-Moon"  # 大小写不合并

    def test_replace_then_list(self, factory):
        db = factory()
        try:
            links = crud.replace_content_entity_links(
                db,
                1,
                [
                    ("晓枣", crud.ENTITY_TYPE_PERSON, "原作"),
                    ("动画工房", crud.ENTITY_TYPE_COMPANY, "动画制作"),
                ],
            )
            assert links == [
                ("晓枣", crud.ENTITY_TYPE_PERSON, "原作"),
                ("动画工房", crud.ENTITY_TYPE_COMPANY, "动画制作"),
            ]
            assert crud.list_content_entity_roles(db, 1) == {
                "原作": ["晓枣"],
                "动画制作": ["动画工房"],
            }
            assert crud.count_content_entity_links(db, 1) == 2
        finally:
            db.close()

    def test_replace_is_not_additive(self, factory):
        db = factory()
        try:
            crud.replace_content_entity_links(db, 1, [("晓枣", "person", "原作")])
            crud.replace_content_entity_links(db, 1, [("金崎贵臣", "person", "导演")])
            assert crud.list_content_entity_roles(db, 1) == {"导演": ["金崎贵臣"]}
            assert crud.count_content_entity_links(db, 1) == 1
        finally:
            db.close()

    def test_empty_links_clear(self, factory):
        db = factory()
        try:
            crud.replace_content_entity_links(db, 1, [("晓枣", "person", "原作")])
            crud.replace_content_entity_links(db, 1, [])
            assert crud.list_content_entity_links(db, 1) == []
        finally:
            db.close()

    def test_entity_rows_reused(self, factory):
        db = factory()
        try:
            crud.replace_content_entity_links(db, 1, [("晓枣", "person", "原作")])
            crud.replace_content_entity_links(db, 2, [("晓枣", "person", "原作")])
            assert db.scalar(text("SELECT COUNT(*) FROM entities")) == 1
            assert crud.count_content_entity_links(db, 2) == 1
        finally:
            db.close()

    def test_overlong_name_dropped(self, factory):
        db = factory()
        try:
            links = crud.replace_content_entity_links(
                db, 1, [("x" * (crud.ENTITY_NAME_MAX_LENGTH + 1), "person", "原作")]
            )
            assert links == []
        finally:
            db.close()

    def test_list_content_entity_links_shape(self, factory):
        db = factory()
        try:
            crud.replace_content_entity_links(
                db, 1, [("晓枣", "person", "原作"), ("动画工房", "company", "动画制作")]
            )
            rows = crud.list_content_entity_links(db, 1)
            assert [row[1:] for row in rows] == [("原作", "晓枣"), ("动画制作", "动画工房")]
            assert all(isinstance(row[0], int) for row in rows)
        finally:
            db.close()

    def test_delete_single_link(self, factory):
        db = factory()
        try:
            crud.replace_content_entity_links(db, 1, [("晓枣", "person", "原作")])
            link_id = crud.list_content_entity_links(db, 1)[0][0]
            assert crud.delete_content_entity_link(db, link_id) is True
            assert crud.delete_content_entity_link(db, link_id) is False
            assert crud.count_content_entity_links(db, 1) == 0
        finally:
            db.close()

    def test_add_link_idempotent(self, factory):
        db = factory()
        try:
            ok, message = crud.add_content_entity_link(db, 1, "晓枣", "原作")
            assert ok is True
            ok2, message2 = crud.add_content_entity_link(db, 1, "晓枣", "原作")
            assert ok2 is True
            assert "已存在" in message2
            assert crud.count_content_entity_links(db, 1) == 1
            ok3, _ = crud.add_content_entity_link(db, 1, "  ", "原作")
            assert ok3 is False
        finally:
            db.close()

    def test_list_entities_with_counts(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            rows = crud.list_entities_with_counts(db)
            # 计数相同按名称排序（SQLite 字典序），不是识别顺序
            assert [entity.name for entity, _ in rows] == ["动画工房", "晓枣"]
            assert [count for _, count in rows] == [1, 1]
            companies = crud.list_entities_with_counts(db, entity_type=crud.ENTITY_TYPE_COMPANY)
            assert [entity.name for entity, _ in companies] == ["动画工房"]
            assert crud.get_entity_by_name(db, "晓枣") is not None
            assert crud.get_entity_by_name(db, "不存在的人") is None
        finally:
            db.close()

    def test_list_entity_contents_distinct_and_ordered(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            entity = crud.get_entity_by_name(db, "晓枣")
            # 同一部作品再挂一个「晓枣」的其它角色：不应让作品出现两次
            crud.add_content_entity_link(db, 1, "晓枣", "系列构成")
            contents = crud.list_entity_contents(db, entity.id)
            assert [content.id for content in contents] == [1]
        finally:
            db.close()


class TestSeriesCrud:
    def test_set_and_clear_series(self, factory):
        db = factory()
        try:
            assert crud.set_content_series(db, 1, "为美好的世界献上祝福") == "为美好的世界献上祝福"
            content = db.get(Content, 1)
            assert content.series_id is not None
            assert crud.set_content_series(db, 1, "") is None
            db.refresh(content)
            assert content.series_id is None
        finally:
            db.close()

    def test_set_series_missing_content(self, factory):
        db = factory()
        try:
            assert crud.set_content_series(db, 999, "某系列") is None
        finally:
            db.close()

    def test_series_row_reused(self, factory):
        db = factory()
        try:
            crud.set_content_series(db, 1, "同一系列")
            crud.set_content_series(db, 2, "同一系列")
            assert db.scalar(text("SELECT COUNT(*) FROM series")) == 1
        finally:
            db.close()

    def test_list_series_with_counts_and_contents(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            rows = crud.list_series_with_counts(db)
            assert [(series.name, count) for series, count in rows] == [
                ("为美好的世界献上祝福", 2)
            ]
            series = crud.get_series_by_name(db, "为美好的世界献上祝福")
            contents = crud.list_series_contents(db, series.id, exclude_content_id=1)
            assert [content.id for content in contents] == [2]
            assert crud.get_series(db, series.id).name == "为美好的世界献上祝福"
        finally:
            db.close()

    def test_has_recognition(self, factory):
        db = factory()
        try:
            assert crud.has_content_entity_recognition(db, 1) is False
            crud.set_content_series(db, 1, "某系列")
            assert crud.has_content_entity_recognition(db, 1) is True
        finally:
            db.close()

    def test_ensure_series_column_idempotent(self, factory):
        engine = factory.kw["bind"]
        assert crud.ensure_content_series_column(engine) == []
        assert crud.ensure_content_series_column(engine) == []


# ---------- 3. 搜索筛选 ----------


class TestCreatorSearch:
    def test_filter_by_creator(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            total, contents = crud.search_contents_page(db, creator="晓枣")
            assert total == 1
            assert contents[0].id == 1
            total2, contents2 = crud.search_contents_page(db, creator="不存在的作者")
            assert (total2, contents2) == (0, [])
        finally:
            db.close()

    def test_creator_with_tag_no_row_duplication(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            crud.replace_content_tags(db, 1, ["喜剧"])
            # 同一作品既有标签又有多角色同一实体：不应出现两行
            crud.add_content_entity_link(db, 1, "晓枣", "系列构成")
            total, contents = crud.search_contents_page(db, tag="喜剧", creator="晓枣")
            assert total == 1
            assert [content.id for content in contents] == [1]
        finally:
            db.close()

    def test_search_contents_signature(self, factory):
        _recognized_factory(factory)
        db = factory()
        try:
            assert [c.id for c in crud.search_contents(db, "", creator="动画工房")] == [1]
        finally:
            db.close()


# ---------- 4. 导入集成 ----------


class TestImportIntegration:
    def test_entities_written_on_import(self, factory):
        from scripts import import_bangumi

        payload = _infobox_payload(
            subject_id=555,
            infobox=[
                {"key": "原作", "value": "晓枣"},
                {"key": "动画制作", "value": "动画工房"},
            ],
        )
        summary = import_bangumi.run_import(
            [555],
            session_factory=factory,
            apply=True,
            fetch=lambda subject_id: payload,
            cover_downloader=False,
        )
        assert summary.imported == 1
        reason = summary.outcomes[0].reason
        assert "作者/公司 2 项" in reason
        db = factory()
        try:
            content = db.scalar(select(Content).where(Content.bangumi_id == 555))
            assert crud.list_content_entity_roles(db, content.id) == {
                "原作": ["晓枣"],
                "动画制作": ["动画工房"],
            }
        finally:
            db.close()

    def test_no_infobox_keys_is_silent(self, factory):
        from scripts import import_bangumi

        payload = _infobox_payload(
            subject_id=101, infobox=[{"key": "价格", "value": "¥ 680"}]
        )
        summary = import_bangumi.run_import(
            [101],
            session_factory=factory,
            apply=True,
            fetch=lambda subject_id: payload,
            cover_downloader=False,
        )
        assert summary.imported == 1
        assert "作者/公司" not in summary.outcomes[0].reason
        db = factory()
        try:
            assert crud.count_content_entity_links(db, 1) == 0
        finally:
            db.close()

    def test_infobox_series_written_on_import(self, factory):
        from scripts import import_bangumi

        payload = _infobox_payload(
            subject_id=102, infobox=[{"key": "系列", "value": "某系列"}]
        )
        summary = import_bangumi.run_import(
            [102],
            session_factory=factory,
            apply=True,
            fetch=lambda subject_id: payload,
            cover_downloader=False,
        )
        assert summary.imported == 1
        assert "系列『某系列』" in summary.outcomes[0].reason
        db = factory()
        try:
            content = db.scalar(select(Content).where(Content.bangumi_id == 102))
            series = crud.get_series(db, content.series_id)
            assert series.name == "某系列"
        finally:
            db.close()


# ---------- 5. 回填 CLI ----------


def _cli_fetches(payloads, related=None):
    """构造 (fetch, fetch_related) 假函数；related 缺省返回空列表。"""
    related = related or {}

    def fetch(subject_id):
        return payloads[subject_id]

    def fetch_related(subject_id):
        return related.get(subject_id, [])

    return fetch, fetch_related


class TestBackfillCli:
    def test_preview_then_apply(self, factory):
        payloads = {
            100: _infobox_payload(100, [{"key": "原作", "value": "晓枣"}]),
            99: _infobox_payload(99, [{"key": "导演", "value": "金崎贵臣"}]),
        }
        fetch, fetch_related = _cli_fetches(payloads)

        preview = link_entities.run_backfill(factory, fetch=fetch, fetch_related=fetch_related)
        assert preview.mode == "preview"
        assert preview.requested == 3
        assert preview.pending == 2
        assert preview.skipped == 1  # 没有 bangumi_id 的作品
        db = factory()
        try:
            assert crud.count_content_entity_links(db, 1) == 0
        finally:
            db.close()

        # 预览不写库：第一次 apply 才真正写入
        applied = link_entities.run_backfill(
            factory, apply=True, fetch=fetch, fetch_related=fetch_related
        )
        assert applied.recognized == 2
        assert applied.linked == 2

        # 第二次 apply：已有关系数据，默认跳过（--force 才重算）
        again = link_entities.run_backfill(
            factory, apply=True, fetch=fetch, fetch_related=fetch_related
        )
        assert again.recognized == 0
        assert again.skipped == 3

        forced = link_entities.run_backfill(
            factory, apply=True, force=True, fetch=fetch, fetch_related=fetch_related
        )
        assert forced.recognized == 2
        db = factory()
        try:
            assert crud.list_content_entity_roles(db, 1) == {"原作": ["晓枣"]}
            assert crud.list_content_entity_roles(db, 2) == {"导演": ["金崎贵臣"]}
        finally:
            db.close()

    def test_content_ids_filter(self, factory):
        payloads = {100: _infobox_payload(100, [{"key": "原作", "value": "晓枣"}])}
        fetch, fetch_related = _cli_fetches(payloads)
        summary = link_entities.run_backfill(
            factory, apply=True, content_ids=[1], fetch=fetch, fetch_related=fetch_related
        )
        assert summary.requested == 1
        assert summary.recognized == 1
        assert summary.outcomes[0].content_id == 1

    def test_series_mode_requires_pair_in_db(self, factory):
        payloads = {
            100: _infobox_payload(100, [{"key": "原作", "value": "晓枣"}]),
            99: _infobox_payload(99, []),
        }
        related = {
            100: [
                {"id": 99, "relation": "续集", "name_cn": "为美好的世界献上祝福 第一季"},
                {"id": 55, "relation": "续集", "name_cn": "不在库里的作品"},
            ],
            99: [],
        }
        fetch, fetch_related = _cli_fetches(payloads, related)
        summary = link_entities.run_backfill(
            factory, apply=True, series=True, fetch=fetch, fetch_related=fetch_related
        )
        # 只有 bangumi 100 有可用配对；99 的关联列表为空，不归系列
        assert summary.series_set == 1
        db = factory()
        try:
            first = db.get(Content, 1)
            second = db.get(Content, 2)
            assert first.series_id is not None
            assert second.series_id is None
            series = crud.get_series(db, first.series_id)
            assert series.name == "为美好的世界献上祝福"
        finally:
            db.close()

    def test_series_mode_ignores_non_series_relations(self, factory):
        payloads = {100: _infobox_payload(100, [])}
        related = {100: [{"id": 99, "relation": "不同演绎", "name_cn": "为美好的世界献上祝福 第一季"}]}
        fetch, fetch_related = _cli_fetches(payloads, related)
        summary = link_entities.run_backfill(
            factory, series=True, fetch=fetch, fetch_related=fetch_related
        )
        assert summary.series_set == 0

    def test_series_fetch_failure_does_not_block_links(self, factory):
        payloads = {
            100: _infobox_payload(100, [{"key": "原作", "value": "晓枣"}]),
            99: _infobox_payload(99, [{"key": "导演", "value": "金崎贵臣"}]),
        }

        def fetch_related(subject_id):
            raise RuntimeError("网络不通")

        summary = link_entities.run_backfill(
            factory,
            apply=True,
            series=True,
            fetch=lambda subject_id: payloads[subject_id],
            fetch_related=fetch_related,
        )
        assert summary.recognized == 2
        assert summary.failed == 0
        handled = [o for o in summary.outcomes if o.status != "skipped"]
        assert handled and all("关联条目获取失败" in o.reason for o in handled)

    def test_fetch_failure_is_failed(self, factory):
        def fetch(subject_id):
            raise RuntimeError("网络不通")

        summary = link_entities.run_backfill(factory, fetch=fetch, fetch_related=fetch)
        assert summary.requested == 3
        # 没有 bangumi_id 的作品在拉取前就跳过，不记为失败
        assert summary.failed == 2
        assert all(outcome.status == "failed" for outcome in summary.outcomes if outcome.status != "skipped")

    def test_exit_codes(self, factory, capsys):
        payloads = {100: _infobox_payload(100, [])}
        fetch, fetch_related = _cli_fetches(payloads)
        assert (
            link_entities.main(
                ["--content-id", "9999"], session_factory=factory
            )
            == link_entities.EXIT_NO_MATCH
        )
        assert (
            link_entities.main(
                ["--content-id", "0"], session_factory=factory
            )
            == link_entities.EXIT_USAGE
        )
        assert (
            link_entities.main([], session_factory=factory) == link_entities.EXIT_OK
        )


# ---------- 6. 页面 ----------


class TestCreatorPages:
    def test_index_empty_state(self, page_env):
        resp = page_env.client.get("/creators")
        assert resp.status_code == 200
        assert "还没有识别到任何作者/公司关联" in resp.text

    def test_index_lists_entities_and_series(self, page_env):
        _recognized_factory(page_env.session_factory)
        resp = page_env.client.get("/creators")
        assert resp.status_code == 200
        assert "晓枣" in resp.text
        assert "动画工房" in resp.text
        assert "为美好的世界献上祝福" in resp.text

    def test_entity_detail(self, page_env):
        _recognized_factory(page_env.session_factory)
        resp = page_env.client.get("/creators/" + "晓枣")
        assert resp.status_code == 200
        assert "为美好的世界献上祝福 第二季" in resp.text
        assert "/contents?creator=" in resp.text

    def test_series_detail(self, page_env):
        _recognized_factory(page_env.session_factory)
        resp = page_env.client.get("/creators/" + "为美好的世界献上祝福")
        assert resp.status_code == 200
        assert "为美好的世界献上祝福 第一季" in resp.text
        assert "为美好的世界献上祝福 第二季" in resp.text

    def test_unknown_creator_is_404(self, page_env):
        resp = page_env.client.get("/creators/" + "查无此人")
        assert resp.status_code == 404
        assert "作者/公司" in resp.text

    def test_contents_creator_filter(self, page_env):
        _recognized_factory(page_env.session_factory)
        resp = page_env.client.get("/contents?creator=" + "晓枣")
        assert resp.status_code == 200
        assert "为美好的世界献上祝福 第二季" in resp.text
        assert "孤独的作品" not in resp.text
        assert "作者/公司：晓枣" in resp.text

    def test_detail_page_shows_relations_and_series(self, page_env):
        _recognized_factory(page_env.session_factory)
        resp = page_env.client.get("/contents/1")
        assert resp.status_code == 200
        assert "/creators/" in resp.text
        assert "原作" in resp.text
        assert "系列作品" in resp.text
        assert "为美好的世界献上祝福 第一季" in resp.text


class TestManageEntitiesPage:
    def test_guest_redirected_to_login(self, page_env):
        resp = page_env.client.get("/manage/entities")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/manage/entities"

    def test_user_forbidden(self, page_env):
        login_as_user(page_env.client)
        resp = page_env.client.get("/manage/entities")
        assert resp.status_code == 403

    def test_admin_sees_page_and_rows(self, page_env):
        login_as_admin(page_env.client)
        resp = page_env.client.get("/manage/entities")
        assert resp.status_code == 200
        assert "关联管理" in resp.text
        assert "为美好的世界献上祝福 第二季" in resp.text
        assert "/manage/entities/link/add" in resp.text

    def test_add_and_delete_link(self, page_env):
        login_as_admin(page_env.client)
        resp = page_env.client.post(
            "/manage/entities/link/add",
            data={"content_id": 3, "name": "手动作者", "role": "原作"},
        )
        assert resp.status_code == 303
        assert "note=added" in resp.headers["location"]
        db = page_env.session_factory()
        try:
            assert crud.list_content_entity_roles(db, 3) == {"原作": ["手动作者"]}
        finally:
            db.close()

        link_id = crud.list_content_entity_links(page_env.session_factory(), 3)[0][0]
        resp = page_env.client.post(
            "/manage/entities/link/delete", data={"link_id": link_id}
        )
        assert resp.status_code == 303
        assert "note=removed" in resp.headers["location"]
        db = page_env.session_factory()
        try:
            assert crud.count_content_entity_links(db, 3) == 0
        finally:
            db.close()

    def test_add_link_empty_name_is_bad_input(self, page_env):
        login_as_admin(page_env.client)
        resp = page_env.client.post(
            "/manage/entities/link/add",
            data={"content_id": 1, "name": "  ", "role": "原作"},
        )
        assert resp.status_code == 303
        assert "note=bad_input" in resp.headers["location"]

    def test_set_and_clear_series(self, page_env):
        login_as_admin(page_env.client)
        resp = page_env.client.post(
            "/manage/entities/series",
            data={"content_id": 3, "series": "手工系列"},
        )
        assert resp.status_code == 303
        assert "note=series_saved" in resp.headers["location"]
        db = page_env.session_factory()
        try:
            series = crud.get_series(db, db.get(Content, 3).series_id)
            assert series.name == "手工系列"
        finally:
            db.close()

        resp = page_env.client.post(
            "/manage/entities/series",
            data={"content_id": 3, "action": "clear"},
        )
        assert resp.status_code == 303
        assert "note=series_cleared" in resp.headers["location"]
        db = page_env.session_factory()
        try:
            assert db.get(Content, 3).series_id is None
        finally:
            db.close()

    def test_note_text_rendered(self, page_env):
        login_as_admin(page_env.client)
        resp = page_env.client.get("/manage/entities?note=added")
        assert resp.status_code == 200
        assert "已添加作者/公司关联" in resp.text
