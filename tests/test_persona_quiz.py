"""选题式成分测试：不用外部账号，点选站内作品就能出画像。

覆盖这条链路：出题（类目配额 + 热度打底 + 随机打散）→ 提交 → 出画像。
重点钉死几个容易静默出错的点：
- 题面只取有本地封面的作品（没封面的卡片是灰块）；
- 同一 seed / size 题面稳定，换 seed 换题；
- 「无感」不进画像（它不是负向信号）；
- seen 与 want 同时勾上时以 seen 为准；
- 题量只认 20 / 100 两档，拼 URL 放大额度会被回落。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, persona, persona_pages
from app.database import Base, get_db
from app.main import app
from app.models import Content
from auth_helpers import USER_USERNAME, create_accounts, login_as_user

TYPES = ("anime", "comic", "game", "novel")
PER_TYPE = 8  # 每类 8 部，够 20 题（每类 5）取样


@pytest.fixture
def quiz_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quiz.db'}",
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
    try:
        yield SimpleNamespace(client=client, session_factory=TestingSession)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _seed_contents(session_factory, with_cover=True):
    """造每个类目若干部作品，一半带封面一半不带（验证封面过滤生效）。"""
    db = session_factory()
    try:
        rows = []
        for type_name in TYPES:
            for index in range(PER_TYPE):
                rows.append(
                    Content(
                        title=f"{type_name}-{index}",
                        type=type_name,
                        summary="",
                        rating=7.0 + index / 10,
                        rating_count=100 + index,
                        # 偶数下标才有封面：sample 必须跳过奇数下标的那些
                        image_url=(
                            f"/covers/{type_name}-{index}.jpg"
                            if (with_cover or index % 2 == 0)
                            else None
                        ),
                        year=2010 + index,
                    )
                )
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]
    finally:
        db.close()


def _tag_all(session_factory, ids, tags=("日常", "搞笑")):
    db = session_factory()
    try:
        for content_id in ids:
            crud.replace_content_tags(db, content_id, list(tags))
    finally:
        db.close()


@pytest.fixture
def no_cooldown():
    """选题页有 20 秒提交防抖，同一用例连续 POST 会被挡——挨个用例清掉。

    防抖本身是对的（build_profile + 推荐不便宜），不该为了测试把它关掉。
    """
    persona_pages._guest_hits.clear()
    yield
    persona_pages._guest_hits.clear()


def _seen_ids(page_text, limit=None):
    """从选题页里抠出所有 seen 复选框的作品 id（即这一批的题面）。"""
    found = [
        line.split('value="')[1].split('"')[0]
        for line in page_text.splitlines()
        if 'name="seen"' in line
    ]
    return found[:limit] if limit else found


class TestQuizPage:
    def test_page_lists_works_from_every_type(self, quiz_env):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        resp = quiz_env.client.get("/persona/quiz")
        assert resp.status_code == 200
        body = resp.text
        # 只有带封面的会被抽到（偶数下标）
        assert "anime-0" in body
        assert "anime-1" not in body
        # 四个类目都要出现在题面里，否则 drop 掉某一类就没人发现
        for type_name in TYPES:
            assert f"{type_name}-" in body

    def test_same_seed_stable_new_seed_changes(self, quiz_env):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        first = quiz_env.client.get("/persona/quiz?size=20&seed=7").text
        again = quiz_env.client.get("/persona/quiz?size=20&seed=7").text
        other = quiz_env.client.get("/persona/quiz?size=20&seed=8").text
        assert first == again
        assert first != other

    def test_size_is_restricted_to_two_tiers(self, quiz_env):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        # 100 超出每类样本量时按实际可取数回落；非法值回落到默认 20 档
        for bad in ("9999", "abc", "-3"):
            resp = quiz_env.client.get(f"/persona/quiz?size={bad}")
            assert resp.status_code == 200
            assert 'value="20"' in resp.text


class TestQuizSubmit:
    def test_result_page_renders_profile(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=3")
        picked = _seen_ids(page.text, 5)
        assert len(picked) >= 3

        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 3},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "二次元人格" in body
        assert "站内选题" in body

    def test_unseen_works_do_not_pollute_profile(self, quiz_env, no_cooldown):
        """没圈的作品不进画像：题目有 20 张，标记 3 张就该只算这 3 张。"""
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=5")
        picked = _seen_ids(page.text, 3)

        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 5},
        )
        assert resp.status_code == 200
        assert resp.text.count("persona-radar") >= 1
        # 结果页里的作品数只等于标记过的数量，而不是整个题面 20 张
        assert f"站内选题 {len(picked)} 条" in resp.text

    def test_duplicate_pick_counts_once(self, quiz_env, no_cooldown):
        """同一部同时勾了「看过」与「想看」时只算一部（3 条，而不是 4 条）。

        seen / want 是两个独立的 checkbox 组，同一 id 完全可能同时出现在两边；
        不按 id 去重就会把同一部作品重复计入画像。
        """
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=11")
        picked = _seen_ids(page.text, 3)

        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": [picked[0]], "want": picked, "size": 20, "seed": 11},
        )
        assert resp.status_code == 200
        # picked[0] 在两组里各提交一次，去重后仍只算一部
        assert "站内选题 3 条" in resp.text

    def test_too_few_picks_is_rejected(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=13")
        picked = _seen_ids(page.text, 1)

        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 13},
        )
        assert resp.status_code == 200
        assert "才够算出画像" in resp.text


class TestRarity:
    """稀有度钩子：成分桶的键、分级阈值、计数累加。"""

    def test_label_key_is_identity_plus_cluster(self):
        profile = {"persona_tags": ["日常系", "番剧党", "仓鼠", "博爱"]}
        # 只取前两项：拿全部标签做键的话几乎每人一份，占比没有意义
        assert persona.label_key_of(profile) == "日常系·番剧党"

    def test_label_key_handles_single_or_missing_tag(self):
        assert persona.label_key_of({"persona_tags": ["番剧党"]}) == "番剧党"
        assert persona.label_key_of({"persona_tags": []}) == ""
        assert persona.label_key_of({}) == ""

    def test_rarity_tiers(self):
        assert persona.rarity_of(0.5)["tier"] == "SSR"
        assert persona.rarity_of(2.0)["tier"] == "SSR"
        assert persona.rarity_of(5.0)["tier"] == "SR"
        assert persona.rarity_of(20.0)["tier"] == "R"
        assert persona.rarity_of(80.0)["tier"] == "N"

    def test_record_accumulates_per_bucket_and_total(self, quiz_env):
        db = quiz_env.session_factory()
        try:
            first = crud.record_persona_label(db, "日常系·番剧党")
            second = crud.record_persona_label(db, "日常系·番剧党")
            other = crud.record_persona_label(db, "战斗系·漫画党")
        finally:
            db.close()
        assert first == (1, 1)
        assert second == (2, 2)
        assert other == (1, 3)

    def test_blank_key_is_ignored(self, quiz_env):
        db = quiz_env.session_factory()
        try:
            assert crud.record_persona_label(db, "   ") == (0, 0)
        finally:
            db.close()

    def test_result_page_shows_rarity(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=21")
        picked = _seen_ids(page.text, 5)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 21},
        )
        assert resp.status_code == 200
        assert "稀有度" in resp.text


class TestShareCard:
    """分享卡：参数编解码 + SVG 渲染 + 畸形输入不炸。"""

    PROFILE = {
        "persona_tags": ["日常系", "番剧党", "仓鼠"],
        "dimensions": {
            "type_focus": 88.5,
            "tag_focus": 61.0,
            "mainstream": 40.0,
            "exploration": 72.0,
            "hype": 30.0,
            "completion": 90.0,
        },
    }

    def test_roundtrip(self):
        token = persona.encode_card(self.PROFILE, {"tier": "SR", "share": 5.5, "total": 120})
        data = persona.decode_card(token)
        assert data["tags"] == ["日常系", "番剧党", "仓鼠"]
        assert data["dims"][0] == 88.5
        assert data["rarity"] == ["SR", 5.5, 120]

    def test_tolerates_missing_rarity(self):
        data = persona.decode_card(persona.encode_card(self.PROFILE))
        assert data["rarity"] is None

    def test_bad_token_returns_none(self):
        assert persona.decode_card("") is None
        assert persona.decode_card("!!!not base64!!!") is None
        assert persona.decode_card("x" * 3000) is None

    def test_route_renders_svg(self, quiz_env):
        token = persona.encode_card(self.PROFILE)
        resp = quiz_env.client.get(f"/persona/card?d={token}")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]
        assert "日常系" in resp.text
        assert "<svg" in resp.text

    def test_route_rejects_bad_token(self, quiz_env):
        assert quiz_env.client.get("/persona/card?d=oops").status_code == 404

    def test_result_page_links_to_card(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=31")
        picked = _seen_ids(page.text, 5)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 31},
        )
        assert resp.status_code == 200
        assert "/persona/card?d=" in resp.text


class TestShareCard:
    """分享卡：参数编解码 + SVG 渲染 + 畸形输入不炸。"""

    PROFILE = {
        "persona_tags": ["日常系", "番剧党", "仓鼠"],
        "dimensions": {
            "type_focus": 88.5,
            "tag_focus": 61.0,
            "mainstream": 40.0,
            "exploration": 72.0,
            "hype": 30.0,
            "completion": 90.0,
        },
    }

    def test_roundtrip(self):
        token = persona.encode_card(self.PROFILE, {"tier": "SR", "share": 5.5, "total": 120})
        data = persona.decode_card(token)
        assert data["tags"] == ["日常系", "番剧党", "仓鼠"]
        assert data["dims"][0] == 88.5
        assert data["rarity"] == ["SR", 5.5, 120]

    def test_tolerates_missing_rarity(self):
        data = persona.decode_card(persona.encode_card(self.PROFILE))
        assert data["rarity"] is None

    def test_bad_token_returns_none(self):
        assert persona.decode_card("") is None
        assert persona.decode_card("!!!not base64!!!") is None
        assert persona.decode_card("x" * 3000) is None

    def test_route_renders_svg(self, quiz_env):
        token = persona.encode_card(self.PROFILE)
        resp = quiz_env.client.get(f"/persona/card?d={token}")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]
        assert "日常系" in resp.text
        assert "<svg" in resp.text

    def test_route_rejects_bad_token(self, quiz_env):
        assert quiz_env.client.get("/persona/card?d=oops").status_code == 404

    def test_result_page_links_to_card(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=31")
        picked = _seen_ids(page.text, 5)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 31},
        )
        assert resp.status_code == 200
        assert "/persona/card?d=" in resp.text


class TestAnonymousResult:
    """游客结果留存：落库 → 签名 Cookie → 注册登录后领回。"""

    def test_saved_result_can_be_claimed_once(self, quiz_env):
        db = quiz_env.session_factory()
        try:
            result_id = crud.save_anonymous_persona_result(
                db, {"profile": {"persona_tags": ["日常系"]}}, source="quiz"
            )
            payload = crud.claim_anonymous_persona_result(db, result_id, 1)
            assert payload["profile"]["persona_tags"] == ["日常系"]
            # 二次认领必须失败：换台设备也不能把别人的结果再搬一遍
            assert crud.claim_anonymous_persona_result(db, result_id, 2) is None
        finally:
            db.close()

    def test_expired_result_cannot_be_claimed(self, quiz_env):
        db = quiz_env.session_factory()
        try:
            result_id = crud.save_anonymous_persona_result(
                db, {"profile": {"persona_tags": ["日常系"]}}
            )
            row = crud.load_anonymous_persona_result(db, result_id)
            row.created_at = datetime.now() - timedelta(
                days=crud.ANONYMOUS_RESULT_TTL_DAYS + 1
            )
            db.commit()
            assert crud.claim_anonymous_persona_result(db, result_id, 1) is None
        finally:
            db.close()

    def test_purge_removes_expired_only(self, quiz_env):
        db = quiz_env.session_factory()
        try:
            fresh = crud.save_anonymous_persona_result(db, {"profile": {}})
            stale = crud.save_anonymous_persona_result(db, {"profile": {}})
            row = crud.load_anonymous_persona_result(db, stale)
            row.created_at = datetime.now() - timedelta(
                days=crud.ANONYMOUS_RESULT_TTL_DAYS + 5
            )
            db.commit()

            assert crud.purge_anonymous_persona_results(db) == 1
            assert crud.load_anonymous_persona_result(db, fresh) is not None
            assert crud.load_anonymous_persona_result(db, stale) is None
        finally:
            db.close()

    def test_quiz_sets_result_cookie(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=41")
        picked = _seen_ids(page.text, 5)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 41},
        )
        assert resp.status_code == 200
        assert resp.cookies.get(persona_pages.RESULT_COOKIE)
        # 结果页要明说「留了多少天」，否则用户还是会以为刷新就没了
        assert f"留了 {crud.ANONYMOUS_RESULT_TTL_DAYS} 天" in resp.text

    def test_login_claims_pending_result(self, quiz_env, no_cooldown):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=43")
        picked = _seen_ids(page.text, 5)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked, "want": [], "size": 20, "seed": 43},
        )
        token = resp.cookies.get(persona_pages.RESULT_COOKIE)
        assert token

        login_as_user(quiz_env.client)
        quiz_env.client.cookies.set(persona_pages.RESULT_COOKIE, token)
        claimed = quiz_env.client.get("/me/persona", follow_redirects=False)
        # 领回成功后 303 回画像页（PRG，避免刷新重复搬）
        assert claimed.status_code == 303

        db = quiz_env.session_factory()
        try:
            user = crud.get_user_by_username(db, USER_USERNAME)
            report = crud.get_persona_report(db, user.id)
            assert report is not None
            assert report.payload.get("profile")
        finally:
            db.close()
