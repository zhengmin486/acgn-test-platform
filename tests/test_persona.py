"""阿宅画像：Bangumi 归一化、规则画像、LLM 提示词与页面路由。

覆盖：
- Bangumi 条目类型 / 收藏状态映射（私有、音乐 / 三次元被过滤）；
- 画像：类型分布、标签、避雷（抛弃 / 低分）、六维分数、人格标签；
- 提示词包含事实与语气，且明确禁止编造；
- 页面：游客跳登录、同步（打桩不触网）、生成、贴回文案、LLM 生成、清除；
- 雷达图坐标数量。
"""

from types import SimpleNamespace

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import crud, persona, persona_pages
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Content, ContentFavorite, ContentStatus
from auth_helpers import USER_USERNAME, create_accounts, login_as_user

COLLECTIONS = [
    {
        "subject_id": 1,
        "subject_type": 2,
        "name": "凉宫春日的忧郁",
        "name_cn": "凉宫春日的忧郁",
        "date": "2006-04-02",
        "score": 8.4,
        "rank": 100,
        "collection_total": 50000,
        "rate": 9,
        "collection_type": 3,
        "private": False,
        "subject_tags": [
            {"name": "日常", "count": 10},
            {"name": "京都动画", "count": 8},
        ],
        "user_tags": [],
        "updated_at": None,
    },
    {
        "subject_id": 2,
        "subject_type": 2,
        "name": "败犬女主太多了",
        "name_cn": "败犬女主太多了",
        "date": "2024-07-01",
        "score": 8.0,
        "rank": 500,
        "collection_total": 30000,
        "rate": 0,
        "collection_type": 1,
        "private": False,
        "subject_tags": [
            {"name": "恋爱", "count": 5},
            {"name": "党争", "count": 3},
        ],
        "user_tags": [],
        "updated_at": None,
    },
    {
        "subject_id": 3,
        "subject_type": 2,
        "name": "某致郁作品",
        "name_cn": "某致郁作品",
        "date": "2015-01-01",
        "score": 9.0,
        "rank": 50,
        "collection_total": 80000,
        "rate": 3,
        "collection_type": 5,
        "private": False,
        "subject_tags": [
            {"name": "致郁", "count": 9},
            {"name": "沉重", "count": 4},
        ],
        "user_tags": [],
        "updated_at": None,
    },
]


@pytest.fixture
def persona_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'persona.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    create_accounts(TestingSession)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, follow_redirects=False)
    login_as_user(client)
    try:
        yield SimpleNamespace(client=client, session_factory=TestingSession)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _uid(session_factory) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, USER_USERNAME).id
    finally:
        db.close()


def _seed_collections(session_factory) -> None:
    uid = _uid(session_factory)
    db = session_factory()
    try:
        crud.replace_bangumi_collections(db, uid, COLLECTIONS)
    finally:
        db.close()


def _generate(env):
    resp = env.client.post(
        "/me/persona/generate", data={"tone": "sharp"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return resp


def _raw_item(index: int) -> dict:
    return {
        "subject_id": index,
        "subject_type": 2,
        "type": 3,
        "rate": 8,
        "private": False,
        "updated_at": "2024-01-01T00:00:00+08:00",
        "subject": {
            "id": index,
            "name": f"作品{index}",
            "name_cn": "",
            "date": "2020-01-01",
            "score": 8.0,
            "rank": 100,
            "collection_total": 1000,
            "tags": [{"name": "日常", "count": 1}],
        },
        "tags": [],
    }


class TestNormalize:
    def test_anime_item(self):
        item = persona.normalize_item(_raw_item(1))
        assert item["subject_type"] == 2
        assert item["collection_type"] == 3
        assert item["rate"] == 8.0
        assert item["subject_tags"][0]["name"] == "日常"

    def test_private_and_music_filtered(self):
        private = _raw_item(1)
        private["private"] = True
        assert persona.normalize_item(private) is None

        music = _raw_item(2)
        music["subject_type"] = 3
        music["subject"]["type"] = 3
        assert persona.normalize_item(music) is None

    def test_book_mapping(self):
        comic = _raw_item(3)
        comic["subject_type"] = 1
        comic["subject"]["tags"] = [{"name": "漫画", "count": 1}]
        comic_item = persona.normalize_item(comic)
        assert persona._site_type(1, comic_item["subject_tags"]) == "comic"

        novel = _raw_item(4)
        novel["subject_type"] = 1
        novel["subject"]["tags"] = [{"name": "轻小说", "count": 1}]
        novel_item = persona.normalize_item(novel)
        assert persona._site_type(1, novel_item["subject_tags"]) == "novel"


class TestProfile:
    def test_avoided_from_drop_and_low_rate(self):
        works = [
            {
                "title": "A",
                "type": "anime",
                "year": 2006,
                "status": "seen",
                "tags": ["日常", "京都动画"],
                "score": 8.4,
                "popularity": 50000,
                "rate": 9,
                "source": "bangumi",
            },
            {
                "title": "B",
                "type": "anime",
                "year": 2024,
                "status": "want",
                "tags": ["恋爱", "党争"],
                "score": 8.0,
                "popularity": 30000,
                "rate": 0,
                "source": "bangumi",
            },
            {
                "title": "C",
                "type": "anime",
                "year": 2015,
                "status": "drop",
                "tags": ["致郁", "沉重"],
                "score": 9.0,
                "popularity": 80000,
                "rate": 3,
                "source": "bangumi",
            },
        ]
        profile = persona.build_profile(works, site_counts={"favorites": 0}, bangumi_total=3)
        avoided = [tag["name"] for tag in profile["avoided_tags"]]
        assert "致郁" in avoided
        assert "日常" not in avoided
        assert profile["drop"] == 1
        assert profile["seen"] == 1
        assert profile["want"] == 1
        assert profile["entry_year"] == 2006
        assert profile["latest_year"] == 2024
        assert profile["identity"] in ("番剧党", "杂食党")
        assert len(profile["dimensions"]) == 6

    def test_prompt_contains_facts_and_guardrails(self):
        profile = persona.build_profile(
            [
                {
                    "title": "A",
                    "type": "anime",
                    "year": 2006,
                    "status": "seen",
                    "tags": ["日常"],
                    "score": 8.4,
                    "popularity": 50000,
                    "rate": 9,
                    "source": "bangumi",
                }
            ],
            bangumi_total=1,
        )
        sharp = persona.build_llm_prompt(profile, "sharp")
        assert "善意锐评" in sharp
        assert "不得编造" in sharp
        assert "日常" in sharp
        assert "2006" in sharp
        gentle = persona.build_llm_prompt(profile, "gentle")
        assert "温和中性" in gentle


class TestFetch:
    def test_pagination(self, monkeypatch):
        calls = []

        def fake_get_json(opener, url, user_agent, timeout, retries=2, sleep=None):
            calls.append(url)
            if "offset=0" in url:
                return {"data": [_raw_item(i) for i in range(50)], "total": 60}
            return {"data": [_raw_item(i) for i in range(50, 60)], "total": 60}

        monkeypatch.setattr(persona, "_get_json", fake_get_json)
        items, total = persona.fetch_user_collections(
            "tester",
            base_url="https://api.bgm.tv",
            user_agent="ua",
            sleep=lambda _: None,
        )
        assert len(items) == 60
        assert total == 60
        assert len(calls) == 2


class TestPersonaPage:
    def test_guest_redirected(self):
        client = TestClient(app, follow_redirects=False)
        resp = client.get("/me/persona")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/me/persona"

    def test_page_opens_for_user(self, persona_env):
        page = persona_env.client.get("/me/persona")
        assert page.status_code == 200
        assert "我的阿宅画像" in page.text
        assert "同步公开收藏" in page.text

    def test_sync_saves_collections(self, persona_env, monkeypatch):
        monkeypatch.setattr(
            persona_pages.persona,
            "fetch_user_collections",
            lambda *args, **kwargs: (COLLECTIONS, len(COLLECTIONS)),
        )
        resp = persona_env.client.post(
            "/me/persona/sync", data={"username": "tester"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "sync=3" in resp.headers["location"]

        db = persona_env.session_factory()
        try:
            assert crud.count_bangumi_collections(db, _uid(persona_env.session_factory)) == 3
            report = crud.get_persona_report(db, _uid(persona_env.session_factory))
            assert report.bangumi_username == "tester"
        finally:
            db.close()

    def test_generate_requires_sync(self, persona_env):
        resp = persona_env.client.post(
            "/me/persona/generate", data={"tone": "sharp"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_generate_and_render(self, persona_env):
        _seed_collections(persona_env.session_factory)
        resp = _generate(persona_env)
        assert "generated=1" in resp.headers["location"]

        page = persona_env.client.get("/me/persona")
        assert "本命类型" in page.text
        assert "避雷类型" in page.text
        assert "致郁" in page.text
        assert "六维画像" in page.text
        assert "复制提示词" in page.text

    def test_manual_narrative_saved(self, persona_env):
        _seed_collections(persona_env.session_factory)
        _generate(persona_env)
        resp = persona_env.client.post(
            "/me/persona/narrative",
            data={"narrative": "## 称号\n手动贴回的文案"},
            follow_redirects=False,
        )
        assert "narrative=manual" in resp.headers["location"]

        db = persona_env.session_factory()
        try:
            report = crud.get_persona_report(db, _uid(persona_env.session_factory))
            assert report.narrative == "## 称号\n手动贴回的文案"
            assert report.narrative_source == "manual"
        finally:
            db.close()

    def test_llm_without_config_redirects_error(self, persona_env):
        _seed_collections(persona_env.session_factory)
        _generate(persona_env)
        resp = persona_env.client.post("/me/persona/llm", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_llm_with_config(self, persona_env, monkeypatch):
        _seed_collections(persona_env.session_factory)
        _generate(persona_env)
        monkeypatch.setattr(settings, "persona_llm_base_url", "https://example.invalid/v1")
        monkeypatch.setattr(settings, "persona_llm_api_key", "test-key")
        monkeypatch.setattr(settings, "persona_llm_model", "test-model")
        monkeypatch.setattr(
            persona_pages.persona, "call_llm", lambda *args, **kwargs: "## 称号\nLLM 文案"
        )

        resp = persona_env.client.post("/me/persona/llm", follow_redirects=False)
        assert "narrative=llm" in resp.headers["location"]
        db = persona_env.session_factory()
        try:
            report = crud.get_persona_report(db, _uid(persona_env.session_factory))
            assert report.narrative == "## 称号\nLLM 文案"
            assert report.narrative_source == "llm"
        finally:
            db.close()

    def test_clear_removes_data(self, persona_env):
        _seed_collections(persona_env.session_factory)
        _generate(persona_env)
        resp = persona_env.client.post("/me/persona/clear", follow_redirects=False)
        assert "cleared=1" in resp.headers["location"]

        db = persona_env.session_factory()
        try:
            uid = _uid(persona_env.session_factory)
            assert crud.count_bangumi_collections(db, uid) == 0
            assert crud.get_persona_report(db, uid) is None
        finally:
            db.close()


class TestRadar:
    def test_radar_shape(self):
        radar = persona.build_radar(
            {
                "type_focus": 50,
                "tag_focus": 30,
                "mainstream": 60,
                "exploration": 40,
                "hype": 20,
                "completion": 80,
            }
        )
        assert len(radar["polygon"].split()) == 6
        assert len(radar["grid"]) == 4
        assert len(radar["axes"]) == 6
        assert len(radar["labels"]) == 6


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_llm_response(monkeypatch, payload: dict) -> dict:
    """把 call_llm 的 opener 换成固定返回，并记录请求。"""
    captured: dict = {}

    def fake_build_opener(proxy: str = ""):
        class _Opener:
            def open(self, request, timeout=None):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data.decode("utf-8"))
                captured["headers"] = dict(request.headers)
                return _FakeResponse(payload)

        return _Opener()

    monkeypatch.setattr(persona, "_build_opener", fake_build_opener)
    return captured


class TestLlmConfig:
    def test_provider_preset_fills_base_and_model(self):
        cfg = persona.resolve_llm_config("deepseek", "", "sk-test", "")
        assert cfg["ready"] is True
        assert cfg["label"] == "DeepSeek"
        assert cfg["base_url"] == "https://api.deepseek.com"
        assert cfg["model"] == "deepseek-chat"
        assert cfg["hint"] == ""

    def test_doubao_requires_endpoint_id(self):
        cfg = persona.resolve_llm_config("doubao", "", "sk-test", "")
        assert cfg["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
        assert cfg["ready"] is False
        assert "ep-" in cfg["hint"]

        ready = persona.resolve_llm_config("doubao", "", "sk-test", "ep-20260101")
        assert ready["ready"] is True
        assert ready["model"] == "ep-20260101"

    def test_glm_default_and_explicit_override(self):
        preset = persona.resolve_llm_config("glm", "", "sk-test", "")
        assert preset["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert preset["model"] == "glm-4-flash"

        override = persona.resolve_llm_config(
            "glm", "https://proxy.example/v1", "sk-test", "glm-4-plus"
        )
        assert override["base_url"] == "https://proxy.example/v1"
        assert override["model"] == "glm-4-plus"
        assert override["label"] == "GLM（智谱）"

    def test_unconfigured_points_to_web_versions(self):
        cfg = persona.resolve_llm_config("", "", "", "")
        assert cfg["ready"] is False
        assert "网页版" in cfg["hint"]

    def test_custom_endpoint_without_provider(self):
        cfg = persona.resolve_llm_config("", "https://example.com/v1/", "k", "m")
        assert cfg["ready"] is True
        assert cfg["base_url"] == "https://example.com/v1"
        assert cfg["label"] == "自定义接口"

    def test_missing_key_hint(self):
        cfg = persona.resolve_llm_config("deepseek", "", "", "")
        assert cfg["ready"] is False
        assert "API Key" in cfg["hint"]


class TestCallLlm:
    def test_posts_to_chat_completions_with_bearer(self, monkeypatch):
        captured = _patch_llm_response(
            monkeypatch,
            {"choices": [{"message": {"content": "## 称号\n正文"}}]},
        )
        text = persona.call_llm(
            "hi",
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            model="deepseek-chat",
            max_tokens=1200,
        )
        assert text == "## 称号\n正文"
        assert captured["url"] == "https://api.deepseek.com/chat/completions"
        assert captured["body"]["model"] == "deepseek-chat"
        assert captured["body"]["max_tokens"] == 1200
        # urllib 会把 header 名首字母大写
        assert captured["headers"].get("Authorization") == "Bearer sk-test"

    def test_base_url_already_chat_completions(self, monkeypatch):
        captured = _patch_llm_response(
            monkeypatch, {"choices": [{"message": {"content": "ok"}}]}
        )
        persona.call_llm(
            "hi",
            base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            api_key="k",
            model="glm-4-flash",
        )
        assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    def test_content_parts_are_joined(self, monkeypatch):
        _patch_llm_response(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "第一段"},
                                {"type": "text", "text": "第二段"},
                            ]
                        }
                    }
                ]
            },
        )
        assert (
            persona.call_llm(
                "hi", base_url="https://example.com/v1", api_key="k", model="m"
            )
            == "第一段第二段"
        )

    def test_reasoning_only_raises(self, monkeypatch):
        _patch_llm_response(
            monkeypatch,
            {"choices": [{"message": {"content": "", "reasoning_content": "思考中"}}]},
        )
        with pytest.raises(persona.PersonaError, match="思考"):
            persona.call_llm(
                "hi", base_url="https://example.com/v1", api_key="k", model="m"
            )

    def test_empty_choices_raises(self, monkeypatch):
        _patch_llm_response(monkeypatch, {"choices": []})
        with pytest.raises(persona.PersonaError):
            persona.call_llm(
                "hi", base_url="https://example.com/v1", api_key="k", model="m"
            )


class TestLlmTestRoute:
    def test_disabled_shows_hint(self, persona_env, monkeypatch):
        monkeypatch.setattr(settings, "persona_llm_provider", "")
        monkeypatch.setattr(settings, "persona_llm_base_url", "")
        monkeypatch.setattr(settings, "persona_llm_api_key", "")
        monkeypatch.setattr(settings, "persona_llm_model", "")
        resp = persona_env.client.post("/me/persona/llm/test", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_ok_reports_provider(self, persona_env, monkeypatch):
        monkeypatch.setattr(settings, "persona_llm_provider", "deepseek")
        monkeypatch.setattr(settings, "persona_llm_base_url", "")
        monkeypatch.setattr(settings, "persona_llm_api_key", "sk-test")
        monkeypatch.setattr(settings, "persona_llm_model", "")
        monkeypatch.setattr(
            persona_pages.persona, "call_llm", lambda *args, **kwargs: "可用"
        )
        resp = persona_env.client.post("/me/persona/llm/test", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "llmtest=" in location
        assert "llmreply=" in location

    def test_page_shows_web_version_hint_when_disabled(self, persona_env, monkeypatch):
        monkeypatch.setattr(settings, "persona_llm_provider", "")
        monkeypatch.setattr(settings, "persona_llm_base_url", "")
        monkeypatch.setattr(settings, "persona_llm_api_key", "")
        monkeypatch.setattr(settings, "persona_llm_model", "")
        _seed_collections(persona_env.session_factory)
        _generate(persona_env)
        page = persona_env.client.get("/me/persona")
        assert "千问 / 豆包 / DeepSeek 网页版" in page.text
        assert "PERSONA_LLM_PROVIDER=deepseek|doubao|glm" in page.text


class TestSiteSync:
    """收藏同步到本站：补作品库 + 镜像收藏 / 标注。"""

    def _db(self, env):
        return env.session_factory()

    def test_import_creates_contents_favorites_and_marks(self, persona_env):
        _seed_collections(persona_env.session_factory)
        resp = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "site=ok" in location
        assert "site_new=3" in location
        assert "site_fav=2" in location
        assert "site_mark=2" in location

        db = self._db(persona_env)
        try:
            uid = _uid(persona_env.session_factory)
            assert crud.count_contents_by_bangumi(db, [1, 2, 3]) == 3
            rows = list(
                db.scalars(select(Content).where(Content.bangumi_id.in_([1, 2, 3])))
            )
            by_bgm = {row.bangumi_id: row for row in rows}
            assert by_bgm[1].type == "anime"
            assert by_bgm[1].rating == pytest.approx(8.4)
            assert "2006 年的动画" in by_bgm[1].summary
            assert "Bangumi 评分 8.4" in by_bgm[1].summary
            assert by_bgm[1].source_url == "https://bgm.tv/subject/1"
            assert "凉宫春日的忧郁" in by_bgm[1].title

            favorites = db.scalars(
                select(ContentFavorite).where(ContentFavorite.user_id == uid)
            ).all()
            assert len(favorites) == 2  # 抛弃的那部不入收藏
            marks = {
                row.content_id: row.status
                for row in db.scalars(
                    select(ContentStatus).where(ContentStatus.user_id == uid)
                )
            }
            assert marks[by_bgm[1].id] == "seen"
            assert marks[by_bgm[2].id] == "want"
            assert by_bgm[3].id not in marks
        finally:
            db.close()

    def test_second_sync_links_without_duplicates(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        resp = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        location = resp.headers["location"]
        assert "site_new=0" in location
        assert "site_linked=3" in location
        assert "site_fav=0" in location
        assert "site_mark=0" in location

        db = self._db(persona_env)
        try:
            assert crud.count_contents_by_bangumi(db, [1, 2, 3]) == 3
        finally:
            db.close()

    def test_manual_mark_not_overwritten(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)

        db = self._db(persona_env)
        try:
            uid = _uid(persona_env.session_factory)
            content = db.scalar(select(Content).where(Content.bangumi_id == 1))
            crud.set_content_status(db, uid, content.id, "unseen")
        finally:
            db.close()

        resp = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        assert "site_mark=0" in resp.headers["location"]

        db = self._db(persona_env)
        try:
            uid = _uid(persona_env.session_factory)
            content = db.scalar(select(Content).where(Content.bangumi_id == 1))
            row = db.scalar(
                select(ContentStatus).where(
                    ContentStatus.user_id == uid,
                    ContentStatus.content_id == content.id,
                )
            )
            assert row.status == "unseen"
        finally:
            db.close()

    def test_limit_stops_at_configured_count(self, persona_env, monkeypatch):
        _seed_collections(persona_env.session_factory)
        monkeypatch.setattr(settings, "persona_site_sync_limit", 2)
        first = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        assert "site_new=2" in first.headers["location"]
        assert "site_limited=1" in first.headers["location"]

        second = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        assert "site_new=1" in second.headers["location"]
        assert "site_limited=0" in second.headers["location"]

    def test_preview_counts_on_page(self, persona_env):
        _seed_collections(persona_env.session_factory)
        page = persona_env.client.get("/me/persona")
        assert "同步到本站作品库" in page.text
        assert "待导入" in page.text
        assert "<strong>3</strong> 部" in page.text

        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        after = persona_env.client.get("/me/persona")
        assert "<strong>0</strong> 部" in after.text

    def test_guest_redirected(self):
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/me/persona/sync-site")
        assert resp.status_code == 303
        # 守卫把 POST 的 next 回落到画像主页，登录后不会落到只支持 POST 的地址
        assert resp.headers["location"] == "/login?next=/me/persona"

    def test_requires_synced_collections(self, persona_env):
        resp = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        assert "error=" in resp.headers["location"]

    def test_custom_works_keep_type_and_tags(self, persona_env):
        db = self._db(persona_env)
        try:
            uid = _uid(persona_env.session_factory)
            stats = crud.import_works_to_site(
                db,
                uid,
                [
                    {
                        "subject_id": 8801,
                        "title": "某漫画",
                        "type": "comic",
                        "type_label": "漫画",
                        "year": 2019,
                        "status": "seen",
                        "tags": ["日常", "推理"],
                        "score": 7.5,
                        "rate": 7,
                    },
                    {"subject_id": 0, "title": "无 id 的脏数据", "type": "anime"},
                ],
                limit=10,
            )
            assert stats["new"] == 1
            assert stats["skipped"] == 1
            content = db.scalar(select(Content).where(Content.bangumi_id == 8801))
            assert content.type == "comic"
            assert "2019 年的漫画" in content.summary
            assert set(crud.list_content_tags(db, content.id)) == {"日常", "推理"}
            assert stats["favorited"] == 1
            assert stats["marked"] == 1
        finally:
            db.close()


class TestSiteImportTrace:
    """导入记录与撤销：安全源免审核的前提是可追溯、可回滚。"""

    def test_imports_are_recorded(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)

        db = persona_env.session_factory()
        try:
            uid = _uid(persona_env.session_factory)
            assert crud.count_user_site_imports(db, uid) == 3
            rows = crud.list_user_site_imports(db, uid, limit=10)
            assert len(rows) == 3
            assert all(isinstance(created_at, object) for _, created_at in rows)
        finally:
            db.close()

    def test_page_lists_my_imports(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        page = persona_env.client.get("/me/persona")
        assert "已由我导入" in page.text
        assert "撤销我的导入" in page.text
        assert "凉宫春日的忧郁" in page.text

    def test_undo_removes_unused_imports(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)

        resp = persona_env.client.post(
            "/me/persona/site-imports/undo", follow_redirects=False
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "undo=ok" in location
        assert "undo_removed=3" in location
        assert "undo_kept=0" in location

        db = persona_env.session_factory()
        try:
            uid = _uid(persona_env.session_factory)
            assert crud.count_contents_by_bangumi(db, [1, 2, 3]) == 0
            assert crud.count_user_site_imports(db, uid) == 0
            assert db.scalars(
                select(ContentFavorite).where(ContentFavorite.user_id == uid)
            ).all() == []
            assert db.scalars(
                select(ContentStatus).where(ContentStatus.user_id == uid)
            ).all() == []
        finally:
            db.close()

    def test_undo_keeps_content_used_by_others(self, persona_env):
        _seed_collections(persona_env.session_factory)
        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)

        db = persona_env.session_factory()
        try:
            content = db.scalar(select(Content).where(Content.bangumi_id == 1))
            db.add(ContentFavorite(user_id=9999, content_id=content.id))
            db.commit()
        finally:
            db.close()

        resp = persona_env.client.post(
            "/me/persona/site-imports/undo", follow_redirects=False
        )
        assert "undo_removed=2" in resp.headers["location"]
        assert "undo_kept=1" in resp.headers["location"]

        db = persona_env.session_factory()
        try:
            assert crud.count_contents_by_bangumi(db, [1]) == 1
            assert crud.count_contents_by_bangumi(db, [2, 3]) == 0
        finally:
            db.close()

    def test_undo_without_imports_is_noop(self, persona_env):
        resp = persona_env.client.post(
            "/me/persona/site-imports/undo", follow_redirects=False
        )
        assert "undo_removed=0" in resp.headers["location"]

    def test_sync_limit_by_role(self, monkeypatch):
        monkeypatch.setattr(settings, "persona_site_sync_limit", 200)
        monkeypatch.setattr(settings, "persona_site_sync_limit_staff", 1000)

        guest = SimpleNamespace(state=SimpleNamespace(current_user=None))
        assert persona_pages._sync_limit(guest) == 200

        staff_user = SimpleNamespace(state=SimpleNamespace(
            current_user=SimpleNamespace(is_staff=True)
        ))
        assert persona_pages._sync_limit(staff_user) == 1000


class TestGuestPreview:
    """免登录试算（引流）：不写库、不建账号，结果只渲染当次页面。"""

    @pytest.fixture(autouse=True)
    def _reset_cooldown(self):
        persona_pages._guest_hits.clear()
        yield
        persona_pages._guest_hits.clear()

    def test_landing_open_to_guests(self):
        client = TestClient(app, follow_redirects=False)
        page = client.get("/persona")
        assert page.status_code == 200
        assert "测测我的阿宅画像" in page.text
        assert "开始试算" in page.text
        assert "不用授权" in page.text

    def test_preview_renders_without_writing(self, persona_env, monkeypatch):
        monkeypatch.setattr(
            persona_pages.persona,
            "fetch_user_collections",
            lambda *args, **kwargs: (COLLECTIONS, len(COLLECTIONS)),
        )
        client = TestClient(app, follow_redirects=False)

        db = persona_env.session_factory()
        try:
            before = db.scalar(select(Content).order_by(Content.id.desc()).limit(1)).id
        finally:
            db.close()

        resp = client.post("/persona/preview", data={"username": "sai"})
        assert resp.status_code == 200
        assert "阿宅画像试算" in resp.text
        assert "本命类型" in resp.text
        assert "免登录试算结果" in resp.text
        assert "登录后继续" in resp.text
        # 游客看不到「同步到本站」的表单
        assert 'action="/me/persona/sync-site"' not in resp.text

        db = persona_env.session_factory()
        try:
            after = db.scalar(select(Content).order_by(Content.id.desc()).limit(1)).id
            assert after == before  # 没有新建作品
            assert crud.count_bangumi_collections(db, _uid(persona_env.session_factory)) == 0
        finally:
            db.close()

    def test_preview_requires_username(self, persona_env):
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/persona/preview", data={"username": "  "})
        assert resp.status_code == 200
        assert "请先填写 Bangumi 用户名" in resp.text

    def test_preview_cooldown(self, persona_env, monkeypatch):
        monkeypatch.setattr(
            persona_pages.persona,
            "fetch_user_collections",
            lambda *args, **kwargs: (COLLECTIONS, len(COLLECTIONS)),
        )
        client = TestClient(app, follow_redirects=False)
        assert client.post("/persona/preview", data={"username": "sai"}).status_code == 200
        second = client.post("/persona/preview", data={"username": "sai"})
        assert "操作有点频繁" in second.text

    def test_preview_hides_site_sync_card(self, persona_env, monkeypatch):
        monkeypatch.setattr(
            persona_pages.persona,
            "fetch_user_collections",
            lambda *args, **kwargs: (COLLECTIONS, len(COLLECTIONS)),
        )
        client = TestClient(app, follow_redirects=False)
        text = client.post("/persona/preview", data={"username": "sai"}).text
        assert "生成画像" not in text
        assert "LLM 文案" not in text


class TestContinueImport:
    def test_continue_button_shows_remaining(self, persona_env, monkeypatch):
        _seed_collections(persona_env.session_factory)
        monkeypatch.setattr(settings, "persona_site_sync_limit", 2)
        first = persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        location = first.headers["location"].replace("http://testserver", "")

        page = persona_env.client.get(location)
        assert "继续导入剩余 1 部" in page.text

        persona_env.client.post("/me/persona/sync-site", follow_redirects=False)
        after = persona_env.client.get("/me/persona?site=ok&site_new=1&site_linked=3&site_fav=0&site_mark=0&site_limited=0")
        assert "继续导入剩余" not in after.text
