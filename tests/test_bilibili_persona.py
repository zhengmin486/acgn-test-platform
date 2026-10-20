"""B站追番表接入成分检测：拉取归一、跨源去重、快照、自动重算、用户级 LLM Key。

覆盖：
- bilibili.normalize_item / error_message / fetch_user_follows（分页、上限、错误分类）；
- persona.work_from_bilibili 状态映射、dedupe_works_across_sources（Bangumi 优先）；
- /me/persona/sync-bilibili：快照覆盖写 + 自动重算（bilibili_total 进画像）；
- 跨源同作品只计一次（Bangumi 版胜出）；
- 用户级 LLM Key：保存 / 优先于站点级 / 脱显 / 生成时用用户 Key / 清除；
- 画像新鲜度提示（双闸）；清除成分数据连带清 B站快照。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import bilibili, crud, persona, persona_pages
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Content, ContentFavorite
from auth_helpers import USER_USERNAME, create_accounts, login_as_user


@pytest.fixture
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bili.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        crud.init_db(engine)
    create_accounts(factory)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, follow_redirects=False)
    login_as_user(client)
    try:
        yield SimpleNamespace(client=client, session_factory=factory)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _uid(session_factory) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, USER_USERNAME).id
    finally:
        db.close()


def _raw_item(season_id: int, title: str, **overrides) -> dict:
    item = {
        "season_id": season_id,
        "title": title,
        "season_type": 1,
        "season_type_name": "番剧",
        "follow_status": 2,
        "rating": {"score": 8.5, "count": 100},
        "publish": {"release_date": "2024-04-01"},
        "stat": {"follow": 1234},
        "styles": ["日常", "百合"],
        "series": {"title": "某系列"},
        "season_title": "第一季",
        "is_finish": False,
    }
    item.update(overrides)
    return item


def _generate(env):
    resp = env.client.post("/me/persona/generate", data={"tone": "sharp"}, follow_redirects=False)
    assert resp.status_code == 303
    return resp


# ---------- bilibili 模块 ----------

class TestNormalizeItem:
    def test_accepts_anime_types_and_maps_fields(self):
        row = bilibili.normalize_item(_raw_item(1, "测试番"))
        assert row["season_id"] == 1
        assert row["title"] == "测试番"
        assert row["tags"] == ["日常", "百合"]
        assert row["rating"] == 8.5
        assert row["year"] == 2024
        assert row["follow_count"] == 1234
        assert row["follow_status"] == 2
        assert row["series_title"] == "某系列"
        assert row["season_title"] == "第一季"

    def test_guochuang_counts_as_anime(self):
        row = bilibili.normalize_item(_raw_item(2, "国创番", season_type=4))
        assert row is not None and row["season_type"] == 4

    def test_rejects_movie_and_empty(self):
        assert bilibili.normalize_item(_raw_item(3, "纪录片", season_type=3)) is None
        assert bilibili.normalize_item({"season_id": 4, "title": "  "}) is None
        assert bilibili.normalize_item({"title": "无 id"}) is None

    def test_missing_optional_fields_tolerated(self):
        row = bilibili.normalize_item(
            {"season_id": 5, "title": "素番", "season_type": 1, "follow_status": 1}
        )
        assert row["tags"] == [] and row["rating"] is None and row["year"] is None


class TestErrorMessage:
    def test_privacy_code_guides_public_setting(self):
        message = bilibili.error_message(53013)
        assert "公开我的追番列表" in message

    def test_risk_control_and_unknown(self):
        assert "-352" in bilibili.error_message(-352)
        assert "B站接口返回错误" in bilibili.error_message(-777, "boom")


class TestFetchUserFollows:
    def test_paginates_until_short_page(self, monkeypatch):
        pages = []

        def fake_get_json(opener, url, user_agent, timeout, retries=2, sleep=None):
            pages.append(url)
            page = 1 if "pn=1" in url else 2
            rows = [_raw_item(i, f"番{i}") for i in range(1, 31)] if page == 1 else [
                _raw_item(99, "番99")
            ]
            return {"code": 0, "data": {"list": rows, "total": 31}}

        monkeypatch.setattr(bilibili, "_get_json", fake_get_json)
        monkeypatch.setattr(bilibili.time, "sleep", lambda *_: None)
        items, total = bilibili.fetch_user_follows("2", user_agent="UA")
        assert total == 31 and len(items) == 31
        assert len(pages) == 2 and "vmid=2" in pages[0] and "pn=2" in pages[1]

    def test_respects_max_items(self, monkeypatch):
        def fake_get_json(opener, url, user_agent, timeout, retries=2, sleep=None):
            rows = [_raw_item(i, f"番{i}") for i in range(1, 31)]
            return {"code": 0, "data": {"list": rows, "total": 500}}

        monkeypatch.setattr(bilibili, "_get_json", fake_get_json)
        monkeypatch.setattr(bilibili.time, "sleep", lambda *_: None)
        items, _total = bilibili.fetch_user_follows("2", max_items=10)
        assert len(items) == 10

    def test_privacy_error_raised_as_persona_error(self, monkeypatch):
        def fake_get_json(opener, url, user_agent, timeout, retries=2, sleep=None):
            return {"code": 53013, "message": "user private"}

        monkeypatch.setattr(bilibili, "_get_json", fake_get_json)
        with pytest.raises(persona.PersonaError) as exc:
            bilibili.fetch_user_follows("2")
        assert "公开我的追番列表" in str(exc.value)

    def test_mid_validation(self):
        with pytest.raises(ValueError):
            bilibili.fetch_user_follows("")
        with pytest.raises(ValueError):
            bilibili.fetch_user_follows("abc")


# ---------- persona 侧：统一结构 / 跨源去重 ----------

class TestWorkFromBilibili:
    def test_status_mapping_matches_bangumi(self):
        db_row = SimpleNamespace(
            title="测试番", follow_status=1, tags=["日常"], rating=8.0,
            follow_count=10, year=2024,
        )
        work = persona.work_from_bilibili(db_row)
        assert work["status"] == "want" and work["type"] == "anime"
        db_row.follow_status = 3
        assert persona.work_from_bilibili(db_row)["status"] == "seen"
        db_row.follow_status = 0
        assert persona.work_from_bilibili(db_row)["status"] == "interest"


class TestCrossSourceDedupe:
    def test_bangumi_wins_and_suffix_stripped(self):
        bangumi = [{"title": "CLANNAD", "source": "bangumi", "tags": ["日常", "剧情"]}]
        bili = [
            {"title": "CLANNAD 第一季 中文配音", "source": "bilibili", "tags": ["剧情"]},
            {"title": "未收录的新番", "source": "bilibili", "tags": ["百合"]},
        ]
        merged = persona.dedupe_works_across_sources(bangumi, bili)
        assert len(merged) == 2
        assert merged[0]["source"] == "bangumi"
        assert merged[1]["title"] == "未收录的新番"

    def test_no_bilibili_works_is_noop(self):
        bangumi = [{"title": "A", "source": "bangumi", "tags": []}]
        assert persona.dedupe_works_across_sources(bangumi, []) == bangumi


# ---------- 路由：同步 + 自动重算 ----------

class TestSyncBilibiliRoute:
    def test_sync_replaces_snapshot_and_regenerates(self, env, monkeypatch):
        normalized = [bilibili.normalize_item(_raw_item(1, "追番番"))]
        monkeypatch.setattr(
            persona_pages.bilibili,
            "fetch_user_follows",
            lambda mid, **kwargs: (normalized, 1),
        )
        resp = env.client.post(
            "/me/persona/sync-bilibili", data={"mid": "2"}, follow_redirects=False
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "bili=1" in location and "generated=1" in location

        db = env.session_factory()
        try:
            uid = _uid(env.session_factory)
            assert crud.count_bilibili_follows(db, uid) == 1
            report = crud.get_persona_report(db, uid)
            assert report.bilibili_mid == "2"
            assert report.payload["profile"]["bilibili_total"] == 1
            assert "B站追番 1 条" in report.payload["report"]["data_note"]
        finally:
            db.close()

    def test_sync_error_keeps_old_snapshot(self, env, monkeypatch):
        def boom(mid, **kwargs):
            raise persona.PersonaError("该用户的追番列表未公开：先打开公开开关")

        monkeypatch.setattr(persona_pages.bilibili, "fetch_user_follows", boom)
        resp = env.client.post(
            "/me/persona/sync-bilibili", data={"mid": "2"}, follow_redirects=False
        )
        assert resp.status_code == 303 and "error=" in resp.headers["location"]
        db = env.session_factory()
        try:
            assert crud.count_bilibili_follows(db, _uid(env.session_factory)) == 0
        finally:
            db.close()

    def test_bangumi_sync_also_regenerates(self, env, monkeypatch):
        monkeypatch.setattr(
            persona_pages.persona,
            "fetch_user_collections",
            lambda username, **kwargs: (
                [
                    {
                        "subject_id": 10,
                        "subject_type": 2,
                        "name": "同步番",
                        "name_cn": "同步番",
                        "date": "2020-01-01",
                        "score": 8.0,
                        "rank": 100,
                        "collection_total": 1000,
                        "rate": 8,
                        "collection_type": 3,
                        "private": False,
                        "subject_tags": [{"name": "日常", "count": 5}],
                        "user_tags": [],
                        "updated_at": None,
                    }
                ],
                1,
            ),
        )
        resp = env.client.post(
            "/me/persona/sync", data={"username": "sai"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "sync=1" in resp.headers["location"]
        assert "generated=1" in resp.headers["location"]
        db = env.session_factory()
        try:
            assert crud.get_persona_report(db, _uid(env.session_factory)) is not None
        finally:
            db.close()

    def test_cross_source_same_work_counted_once(self, env, monkeypatch):
        db = env.session_factory()
        uid = _uid(env.session_factory)
        try:
            crud.replace_bangumi_collections(
                db,
                uid,
                [
                    {
                        "subject_id": 10,
                        "subject_type": 2,
                        "name": "CLANNAD",
                        "name_cn": "CLANNAD",
                        "date": "2007-10-04",
                        "score": 8.0,
                        "rank": 100,
                        "collection_total": 1000,
                        "rate": 9,
                        "collection_type": 3,
                        "private": False,
                        "subject_tags": [{"name": "日常", "count": 5}],
                        "user_tags": [],
                        "updated_at": None,
                    }
                ],
            )
        finally:
            db.close()
        monkeypatch.setattr(
            persona_pages.bilibili,
            "fetch_user_follows",
            lambda mid, **kwargs: (
                [bilibili.normalize_item(_raw_item(20, "CLANNAD 第一季 中文配音"))],
                1,
            ),
        )
        resp = env.client.post(
            "/me/persona/sync-bilibili", data={"mid": "2"}, follow_redirects=False
        )
        assert resp.status_code == 303 and "bili=1" in resp.headers["location"]
        db = env.session_factory()
        try:
            profile = crud.get_persona_report(db, uid).payload["profile"]
            # 两部快照都在，但跨源去重后兴趣作品只计 1 部
            assert profile["bangumi_total"] == 1 and profile["bilibili_total"] == 1
            assert profile["total"] == 1
        finally:
            db.close()


# ---------- 用户级 LLM Key ----------

class TestUserLlmKey:
    def _prepare(self, env):
        db = env.session_factory()
        uid = _uid(env.session_factory)
        try:
            crud.replace_bangumi_collections(
                db,
                uid,
                [
                    {
                        "subject_id": 10,
                        "subject_type": 2,
                        "name": "有数据番",
                        "name_cn": "有数据番",
                        "date": "2020-01-01",
                        "score": 8.0,
                        "rank": 100,
                        "collection_total": 1000,
                        "rate": 8,
                        "collection_type": 3,
                        "private": False,
                        "subject_tags": [{"name": "日常", "count": 5}],
                        "user_tags": [],
                        "updated_at": None,
                    }
                ],
            )
        finally:
            db.close()
        _generate(env)

    def test_save_key_enables_direct_generation(self, env, monkeypatch):
        self._prepare(env)
        monkeypatch.setattr(settings, "persona_llm_api_key", "")
        resp = env.client.post(
            "/me/persona/llm-key",
            data={"provider": "deepseek", "api_key": "sk-userkey123456", "model": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and "llmkey=saved" in resp.headers["location"]
        page = env.client.get("/me/persona")
        assert "sk-u…3456" in page.text  # 脱敏回显
        assert "用已配置的 LLM 生成文案" in page.text
        assert "你的" in page.text  # 来源标识

    def test_user_key_overrides_site_key(self, env, monkeypatch):
        self._prepare(env)
        monkeypatch.setattr(settings, "persona_llm_provider", "deepseek")
        monkeypatch.setattr(settings, "persona_llm_api_key", "sk-sitekey000000")
        captured = {}

        def fake_call_llm(prompt, **kwargs):
            captured.update(kwargs)
            return "生成完成"

        monkeypatch.setattr(persona_pages.persona, "call_llm", fake_call_llm)
        env.client.post(
            "/me/persona/llm-key",
            data={"provider": "deepseek", "api_key": "sk-userkey123456", "model": ""},
            follow_redirects=False,
        )
        resp = env.client.post("/me/persona/llm", follow_redirects=False)
        assert resp.status_code == 303 and "narrative=llm" in resp.headers["location"]
        assert captured["api_key"] == "sk-userkey123456"

    def test_clear_key_falls_back(self, env):
        self._prepare(env)
        env.client.post(
            "/me/persona/llm-key",
            data={"provider": "deepseek", "api_key": "sk-userkey123456", "model": ""},
            follow_redirects=False,
        )
        resp = env.client.post("/me/persona/llm-key/clear", follow_redirects=False)
        assert resp.status_code == 303 and "llmkey=cleared" in resp.headers["location"]
        db = env.session_factory()
        try:
            assert crud.get_user_llm_config(db, _uid(env.session_factory))["api_key"] == ""
        finally:
            db.close()

    def test_empty_key_rejected(self, env):
        resp = env.client.post(
            "/me/persona/llm-key",
            data={"provider": "deepseek", "api_key": "", "model": ""},
            follow_redirects=False,
        )
        assert "error=" in resp.headers["location"]


# ---------- 新鲜度提示 / 清除 ----------

class TestFreshnessAndClear:
    def test_hint_when_site_favorites_far_exceed_profile(self, env):
        db = env.session_factory()
        uid = _uid(env.session_factory)
        try:
            crud.replace_bangumi_collections(
                db,
                uid,
                [
                    {
                        "subject_id": 10 + i,
                        "subject_type": 2,
                        "name": f"基准番{i}",
                        "name_cn": f"基准番{i}",
                        "date": "2020-01-01",
                        "score": 8.0,
                        "rank": 100,
                        "collection_total": 1000,
                        "rate": 8,
                        "collection_type": 3,
                        "private": False,
                        "subject_tags": [{"name": "日常", "count": 5}],
                        "user_tags": [],
                        "updated_at": None,
                    }
                    for i in range(2)
                ],
            )
        finally:
            db.close()
        _generate(env)

        # 之后又在站内收藏了 14 部（2×1.5=3 且 2+10=12，双闸都过）
        ids = {}
        db = env.session_factory()
        try:
            for i in range(14):
                row = Content(title=f"后收藏{i}", type="anime", summary="x", rating=7.0)
                db.add(row)
                db.commit()
                crud.replace_content_tags(db, row.id, ["机战"])
                ids[i] = row.id
                db.add(ContentFavorite(user_id=uid, content_id=row.id, favorited=True))
            db.commit()
        finally:
            db.close()
        page = env.client.get("/me/persona")
        assert "已远超画像规模" in page.text

    def test_clear_removes_bilibili_snapshot(self, env, monkeypatch):
        normalized = [bilibili.normalize_item(_raw_item(1, "待清除番"))]
        monkeypatch.setattr(
            persona_pages.bilibili,
            "fetch_user_follows",
            lambda mid, **kwargs: (normalized, 1),
        )
        env.client.post(
            "/me/persona/sync-bilibili", data={"mid": "2"}, follow_redirects=False
        )
        resp = env.client.post("/me/persona/clear", follow_redirects=False)
        assert resp.status_code == 303
        db = env.session_factory()
        try:
            uid = _uid(env.session_factory)
            assert crud.count_bilibili_follows(db, uid) == 0
            assert crud.get_persona_report(db, uid) is None
        finally:
            db.close()
