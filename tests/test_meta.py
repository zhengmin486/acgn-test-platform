"""Bangumi 元数据补全：客观信息 / 制作人员 / 角色声优。

覆盖：
- build_meta：媒介 / 原作载体 / 话数 / 年份、主创实体、额外职位、长尾过滤、
  声优只取第一位（原版）、客观信息裁剪；
- sync_content_meta：写入列表 / 声优 / 键值 / 公司 / 实体关联，幂等；
- 页面：详情页展示客观信息与声优，/cast/{person_id} 与 /studios/{name} 可访问；
- 历史库补列：init_db 给旧 contents 表补 media / source_material / episodes / meta_fetched_at。
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import bangumi_meta, crud
from app.database import Base, get_db
from app.main import app
from app.models import Content, Entity, ContentEntityLink

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUBJECT = {
    "id": 265,
    "name": "新世紀エヴァンゲリオン",
    "name_cn": "新世纪福音战士",
    "date": "1995-10-04",
    "platform": "TV",
    "eps": 26,
    "infobox": [
        {"key": "导演", "value": "庵野秀明"},
        {"key": "脚本", "value": "庵野秀明(1-3,5-26)、榎戸洋司(2,7)"},
        {"key": "音乐", "value": "鷺巣詩郎"},
        {"key": "人物设定", "value": "貞本義行"},
        {"key": "动画制作", "value": "GAINAX、タツノコプロ"},
        {"key": "原作", "value": "漫画"},
        {"key": "放送星期", "value": "星期三"},
        {"key": "别名", "value": [{"v": "EVA"}, {"v": "新世纪天鹰战士"}, {"v": "Neon Genesis Evangelion"}, {"v": "第四个"}]},
    ],
}

CHARACTERS = [
    {"id": 1, "name": "碇シンジ", "actors": [{"id": 10, "name": "緒方恵美"}, {"id": 11, "name": "Casey Mongillo"}]},
    {"id": 2, "name": "綾波レイ", "actors": [{"id": 12, "name": "林原めぐみ"}, {"id": 13, "name": "刘海霞"}]},
]

PERSONS = [
    {"id": 100, "name": "庵野秀明", "relation": "原画"},
    {"id": 101, "name": "路人甲", "relation": "原画"},
]


class TestBuildMeta:
    def _meta(self):
        return bangumi_meta.build_meta(SUBJECT, CHARACTERS, PERSONS)

    def test_objective_fields(self):
        meta = self._meta()
        assert meta["media"] == "TV"
        assert meta["episodes"] == 26
        assert meta["year"] == 1995
        assert meta["source_material"] == "漫画原作"
        assert meta["studios"] == ["GAINAX", "タツノコプロ"]

    def test_entity_roles(self):
        roles = {(item["role"], item["name"]) for item in self._meta()["entity_roles"]}
        assert ("导演", "庵野秀明") in roles
        assert ("音乐", "鷺巣詩郎") in roles
        assert ("角色设计", "貞本義行") in roles
        assert ("动画制作", "GAINAX") in roles

    def test_extra_credits_strip_annotation(self):
        credits = self._meta()["credits"]
        scripts = [item for item in credits if item["role"] == "脚本"]
        assert [item["name"] for item in scripts] == ["庵野秀明", "榎戸洋司"]
        assert all(item["tier"] == "main" for item in scripts)

    def test_long_roles_stored_as_candidates(self):
        """长尾不再靠名单过滤：先全存候选，作品数够了才转正展示。"""
        credits = self._meta()["credits"]
        candidates = {item["name"] for item in credits if item["tier"] == "candidate"}
        assert candidates == {"庵野秀明", "路人甲"}
        assert not [item for item in credits if item["tier"] == "long"]

    def test_cast_only_original_actor(self):
        cast = self._meta()["cast"]
        actors = {item["actor"] for item in cast}
        assert actors == {"緒方恵美", "林原めぐみ"}
        assert "刘海霞" not in actors

    def test_facts_trimmed(self):
        facts = self._meta()["facts"]
        assert facts["放送星期"] == "星期三"
        assert facts["别名"].count("/") == 2  # 只留前 3 个别名


@pytest.fixture
def meta_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'meta.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    db = factory()
    try:
        content = Content(
            title="新世纪福音战士", type="anime", summary="简介", rating=8.7,
            bangumi_id=265, source_url="https://bgm.tv/subject/265",
        )
        db.add(content)
        db.commit()
        content_id = content.id
    finally:
        db.close()

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, follow_redirects=False)
    try:
        yield {"client": client, "factory": factory, "content_id": content_id}
    finally:
        app.dependency_overrides.pop(get_db, None)


class TestSync:
    def test_writes_all_layers(self, meta_env):
        db = meta_env["factory"]()
        try:
            content = db.get(Content, meta_env["content_id"])
            bangumi_meta.sync_content_meta(
                db, content,
                payload={"subject": SUBJECT, "characters": CHARACTERS, "persons": PERSONS},
            )
            db.refresh(content)
            assert content.media == "TV"
            assert content.episodes == 26
            assert content.year == 1995
            assert content.source_material == "漫画原作"
            assert content.meta_fetched_at is not None

            assert crud.list_content_studios(db, content.id) == ["GAINAX", "タツノコプロ"]
            credits = crud.list_content_credits(db, content.id)
            assert {item.role for item in credits} >= {"脚本"}
            cast = crud.list_content_cast(db, content.id)
            assert [item.actor for item in cast] == ["緒方恵美", "林原めぐみ"]
            facts = dict(crud.list_content_facts(db, content.id))
            assert facts["放送星期"] == "星期三"

            # 主创落了实体关联，/creators 可反查
            links = db.scalars(
                select(ContentEntityLink).where(ContentEntityLink.content_id == content.id)
            ).all()
            assert {link.role for link in links} >= {"导演", "音乐", "动画制作"}
            assert db.scalar(select(Entity).where(Entity.name == "庵野秀明")) is not None
        finally:
            db.close()

    def test_idempotent(self, meta_env):
        db = meta_env["factory"]()
        payload = {"subject": SUBJECT, "characters": CHARACTERS, "persons": PERSONS}
        try:
            content = db.get(Content, meta_env["content_id"])
            bangumi_meta.sync_content_meta(db, content, payload=payload)
            bangumi_meta.sync_content_meta(db, content, payload=payload)
            assert len(crud.list_content_cast(db, content.id)) == 2
            # 2 个脚本 + 2 个原画候选（候选也入库，只是不展示）
            assert len(crud.list_content_credits(db, content.id)) == 4
        finally:
            db.close()


class TestPages:
    def test_detail_shows_cast_and_facts(self, meta_env):
        db = meta_env["factory"]()
        try:
            content = db.get(Content, meta_env["content_id"])
            bangumi_meta.sync_content_meta(
                db, content,
                payload={"subject": SUBJECT, "characters": CHARACTERS, "persons": PERSONS},
            )
        finally:
            db.close()

        page = meta_env["client"].get(f"/contents/{meta_env['content_id']}")
        assert page.status_code == 200
        assert "主要声优" in page.text
        assert "緒方恵美" in page.text
        assert "制作人员" in page.text
        assert "放送星期" in page.text
        assert "TV" in page.text and "26 话" in page.text
        # 原画只有 1 部作品，仍是候选：折叠区不该出现
        assert "更多制作人员" not in page.text

    def test_cast_page(self, meta_env):
        db = meta_env["factory"]()
        try:
            content = db.get(Content, meta_env["content_id"])
            bangumi_meta.sync_content_meta(
                db, content,
                payload={"subject": SUBJECT, "characters": CHARACTERS, "persons": PERSONS},
            )
            person_id = crud.list_content_cast(db, content.id)[0].person_id
        finally:
            db.close()

        page = meta_env["client"].get(f"/cast/{person_id}")
        assert page.status_code == 200
        assert "緒方恵美" in page.text
        assert "新世纪福音战士" in page.text

    def test_studio_page(self, meta_env):
        db = meta_env["factory"]()
        try:
            content = db.get(Content, meta_env["content_id"])
            bangumi_meta.sync_content_meta(
                db, content,
                payload={"subject": SUBJECT, "characters": CHARACTERS, "persons": PERSONS},
            )
        finally:
            db.close()

        page = meta_env["client"].get("/studios/GAINAX")
        assert page.status_code == 200
        assert "新世纪福音战士" in page.text

    def test_missing_person_404(self, meta_env):
        page = meta_env["client"].get("/cast/99999")
        assert page.status_code == 404


class TestLegacyColumns:
    def test_ensure_adds_meta_columns(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy_meta.db'}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE contents (id INTEGER PRIMARY KEY,"
                        " title VARCHAR(200) NOT NULL, type VARCHAR(20) NOT NULL,"
                        " summary TEXT NOT NULL, rating FLOAT NOT NULL)"
                    )
                )
            assert set(crud.ensure_content_meta_columns(engine)) == {
                "media", "source_material", "episodes", "meta_fetched_at",
            }
            assert crud.ensure_content_meta_columns(engine) == []
        finally:
            engine.dispose()


class TestPromotion:
    """长尾转正：本站累计 ≥10 部作品才展示，不维护知名名单。"""

    def test_promotes_at_threshold(self, meta_env):
        db = meta_env["factory"]()
        try:
            from app.models import ContentCredit

            for index in range(10):
                content = Content(
                    title=f"作品{index}", type="anime", summary="x", rating=7.0,
                    bangumi_id=6000 + index,
                )
                db.add(content)
                db.flush()
                db.add(
                    ContentCredit(
                        content_id=content.id, person_id=900, role="原画",
                        name="多产画师", sort_order=0, tier="candidate",
                    )
                )
            db.commit()

            assert crud.promote_staff_candidates(db, threshold=10) == 10
            tiers = list(
                db.scalars(
                    select(ContentCredit.tier).where(ContentCredit.name == "多产画师")
                )
            )
            assert set(tiers) == {"long"}
        finally:
            db.close()

    def test_keeps_candidate_below_threshold(self, meta_env):
        db = meta_env["factory"]()
        try:
            from app.models import ContentCredit

            for index in range(9):
                content = Content(
                    title=f"作品{index}", type="anime", summary="x", rating=7.0,
                    bangumi_id=7000 + index,
                )
                db.add(content)
                db.flush()
                db.add(
                    ContentCredit(
                        content_id=content.id, person_id=901, role="原画",
                        name="长尾画师", sort_order=0, tier="candidate",
                    )
                )
            db.commit()

            assert crud.promote_staff_candidates(db, threshold=10) == 0
            tier = db.scalar(
                select(ContentCredit.tier).where(ContentCredit.name == "长尾画师")
            )
            assert tier == "candidate"
        finally:
            db.close()
