"""选题式成分测试：不用外部账号，点选站内作品就能出画像。

覆盖这条链路：出题（类目配额 + 热度打底 + 随机打散）→ 提交 → 出画像。
重点钉死几个容易静默出错的点：
- 题面只取有本地封面的作品（没封面的卡片是灰块）；
- 同一 seed / size 题面稳定，换 seed 换题；
- 三态标注看过 / 在看 / 抛弃，抛弃是负向信号、不占「至少 N 部正向」额度；
- seen 与 doing 同时勾上时以 seen 为准；
- 题量只认 20 / 100 两档，拼 URL 放大额度会被回落；
- 「搜更多」按标题找作品插进题面，短查询不查库。
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
                        # rating_count 给真实量级（≥ QUIZ_POP_FLOOR），否则被题面热度下限滤掉；
                        # 同时保留梯度（每类 1000~2400）让跨热度分层取样有样本可分。
                        rating_count=1000 + index * 200,
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
    def test_anime_is_main_others_are_extras(self, quiz_env):
        """主题面只出动画；漫画 / 游戏 / 轻小说折进「附加题」；没封面的不进题面。

        2026-10-28 用户定：题卡优先动画，其他类目降级成折叠的可选附加题。
        """
        ids = _seed_contents(quiz_env.session_factory, with_cover=False)
        _tag_all(quiz_env.session_factory, ids)

        resp = quiz_env.client.get("/persona/quiz")
        assert resp.status_code == 200
        body = resp.text

        # 主题面（#quiz-grid 到 #quiz-extras 之间）只有动画
        grid = body.split('id="quiz-grid"')[1].split('id="quiz-extras"')[0]
        assert "anime-0" in grid
        for type_name in ("comic", "game", "novel"):
            assert f"{type_name}-" not in grid

        # 其他三类在折叠的附加题里
        extras = body.split('id="quiz-extras"')[1]
        for type_name in ("comic", "game", "novel"):
            assert f"{type_name}-" in extras

        # 只有带封面的会被抽到（偶数下标），奇数下标哪都不该出现
        assert "anime-1" not in body

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
            data={"seen": picked, "doing": [], "size": 20, "seed": 3},
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 5},
        )
        assert resp.status_code == 200
        assert resp.text.count("persona-radar") >= 1
        # 结果页里的作品数只等于标记过的数量，而不是整个题面 20 张
        assert f"站内选题 {len(picked)} 条" in resp.text

    def test_duplicate_pick_counts_once(self, quiz_env, no_cooldown):
        """同一部同时勾了「看过」与「在看」时只算一部（3 条，而不是 4 条）。

        seen / doing 是两个独立的 checkbox 组，同一 id 完全可能同时出现在两边；
        不按 id 去重就会把同一部作品重复计入画像。
        """
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=11")
        picked = _seen_ids(page.text, 3)

        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": [picked[0]], "doing": picked, "size": 20, "seed": 11},
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 13},
        )
        assert resp.status_code == 200
        assert "才够算出画像" in resp.text


class TestQuizThreeStates:
    """三态标注（看过 / 在看 / 抛弃）与「搜更多」：2026-10-28 补上原设计漏掉的部分。"""

    def test_three_state_buttons_rendered(self, quiz_env):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        body = quiz_env.client.get("/persona/quiz").text
        for label in ("看过", "在看", "抛弃"):
            assert label in body
        # 三个互斥 checkbox 组都得在，少一个前端互斥就漏
        for name in ('name="seen"', 'name="doing"', 'name="drop"'):
            assert name in body

    def test_drop_is_negative_and_not_counted_as_positive(self, quiz_env, no_cooldown):
        """2 看过 + 1 抛弃：正向只有 2，不够 3 → 拒交（抛弃不算正向额度）。"""
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=17")
        picked = _seen_ids(page.text, 3)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked[:2], "drop": [picked[2]], "size": 20, "seed": 17},
        )
        assert resp.status_code == 200
        assert "才够算出画像" in resp.text

    def test_drop_counts_in_profile(self, quiz_env, no_cooldown):
        """3 看过 + 1 抛弃：出结果，画像里带上抛弃数。"""
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        page = quiz_env.client.get("/persona/quiz?size=20&seed=19")
        picked = _seen_ids(page.text, 4)
        resp = quiz_env.client.post(
            "/persona/quiz",
            data={"seen": picked[:3], "drop": [picked[3]], "size": 20, "seed": 19},
        )
        assert resp.status_code == 200
        assert "persona-radar" in resp.text
        # data_note 里的抛弃数（「/ 抛弃 1。」），不认「抛弃」二字本身——按钮也叫这个
        assert "/ 抛弃 1。" in resp.text

    def test_search_container_is_separate_from_quota_grid(self, quiz_env):
        """搜出来的作品进独立容器，不进主题面 #quiz-grid——不占题量。"""
        ids = _seed_contents(quiz_env.session_factory, with_cover=False)
        _tag_all(quiz_env.session_factory, ids)

        body = quiz_env.client.get("/persona/quiz").text
        grid = body.split('id="quiz-grid"')[1].split('id="quiz-extras"')[0]
        assert 'id="quiz-search-results"' not in grid
        assert 'id="quiz-search-results"' in body

    def test_search_returns_matching_works(self, quiz_env):
        ids = _seed_contents(quiz_env.session_factory)
        _tag_all(quiz_env.session_factory, ids)

        resp = quiz_env.client.get("/persona/quiz/search?q=anime")
        assert resp.status_code == 200
        works = resp.json()["works"]
        assert works, "搜「anime」应该命中 anime-* 那批作品"
        assert any(w["title"].startswith("anime-") for w in works)
        # 只回展示要的标量列，不序列化整个实体
        assert set(works[0]) == {"id", "title", "type", "year", "image_url"}

    def test_search_short_query_skips_db(self, quiz_env):
        resp = quiz_env.client.get("/persona/quiz/search?q=a")
        assert resp.status_code == 200
        assert resp.json()["works"] == []


class TestRarity:
    """稀有度钩子：称号桶的键、分级阈值、计数累加。"""

    def test_rarity_tiers(self):
        # 阈值按 90+ 个称号桶划分（旧版是 32 个成分桶，桶多了每个桶更瘦）
        assert persona.rarity_of(0.5)["tier"] == "SSR"
        assert persona.rarity_of(2.0)["tier"] == "SR"
        assert persona.rarity_of(4.0)["tier"] == "R"
        assert persona.rarity_of(9.23)["tier"] == "N"

    def test_unrated_is_not_the_rarest(self):
        """校准样本零命中的称号走未标定，不能算成 0%（0% 会落进最高档 SSR）。"""
        assert persona.rarity_of(None)["tier"] == "?"
        assert persona.rarity_of(None)["label"] == "未标定"
        assert persona.rarity_of(0.0)["tier"] == "SSR"  # 对照：真的 0% 才是最稀有

    def test_title_rarity_prior_is_sane(self):
        """先验表只收录出现过的称号：全为正数，且没有哪个称号能吃掉一成以上。"""
        assert persona.TITLE_RARITY_SHARE
        assert all(value > 0 for value in persona.TITLE_RARITY_SHARE.values())
        assert max(persona.TITLE_RARITY_SHARE.values()) < 10
        assert persona.title_rarity("PIXIU")["tier"] == "SSR"
        # 表里没有的称号 = 校准样本没出现过
        assert persona.title_rarity("NO-SUCH-CODE")["tier"] == "?"

    def test_rarity_carries_caste_label(self):
        """字母之外要带名号：光 SSR 记不住是哪一档（2026-10-30 用户要求）。"""
        assert persona.rarity_of(0.5)["label"] == "婆罗门"
        assert persona.rarity_of(2.0)["label"] == "刹帝利"
        assert persona.rarity_of(4.0)["label"] == "吠舍"
        assert persona.rarity_of(9.23)["label"] == "路人"
        # 最低档不叫「首陀罗」：多数用户都在这一档，不该按种姓最底层称呼
        assert "首陀罗" not in persona.RARITY_LABELS.values()
        assert persona.rarity_label("") == "路人"  # 脏数据不炸

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
            data={"seen": picked, "doing": [], "size": 20, "seed": 21},
        )
        assert resp.status_code == 200
        assert "稀有度" in resp.text
        # 徽章里字母和名号并存：只有 SSR 看不出是哪一档
        assert any(word in resp.text for word in persona.RARITY_LABELS.values())


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
        # dims 顺序随 DIMENSION_LABELS：第 0 项「题材垂直」、第 1 项「探索广度」
        assert data["dims"][0] == 61.0
        assert data["dims"][1] == 72.0
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 31},
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
        # dims 顺序随 DIMENSION_LABELS：第 0 项「题材垂直」、第 1 项「探索广度」
        assert data["dims"][0] == 61.0
        assert data["dims"][1] == 72.0
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 31},
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 41},
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
            data={"seen": picked, "doing": [], "size": 20, "seed": 43},
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


# 每个保底题材组只打一个代表标签，便于断言「题面覆盖了这个口味」
GENRE_TAGS = {
    "热血战斗": "热血",
    "恋爱": "恋爱",
    "日常搞笑": "日常",
    "科幻机战": "科幻",
    "奇幻异世界": "奇幻",
    "治愈": "治愈",
    "悬疑推理": "悬疑",
    "校园青春": "校园",
}


def _seed_quiz_variety(session_factory, per_bucket=5):
    """造一批「跨年代 + 覆盖全部保底题材」的动画，每部只打一个题材标签。"""
    db = session_factory()
    try:
        rows = []
        index = 0
        for label, tag in GENRE_TAGS.items():
            for k in range(per_bucket):
                rows.append(
                    (
                        Content(
                            title=f"v-{label}-{k}",
                            type="anime",
                            summary="",
                            rating=7.0 + k / 10,
                            rating_count=900 + index,
                            image_url=f"/covers/v-{index}.jpg",
                            # 组内年份循环：老番 / 中段 / 新番各有一点，供年份分层取用
                            year=(2005, 2015, 2024)[k % 3],
                        ),
                        tag,
                    )
                )
                index += 1
        db.add_all([row for row, _tag in rows])
        db.commit()
        by_tag = {}
        for row, tag in rows:
            crud.replace_content_tags(db, row.id, [tag])
            by_tag.setdefault(tag, []).append(row.id)
        return by_tag
    finally:
        db.close()


class TestQuizSampling:
    """题面的两条硬约束：年份分层（A）与题材保底（C）。

    两者都是「不写就不会有人发现」的静默失败：纯按热度排时题面会悄悄变成
    「2010s 少年漫改专场」，新番党和冷门口味的人都答不出画像。
    """

    def test_year_buckets_all_present(self, quiz_env):
        _seed_quiz_variety(quiz_env.session_factory)
        db = quiz_env.session_factory()
        try:
            rows = crud.sample_quiz_contents(db, size=20, seed=1)
        finally:
            db.close()

        years = [row["year"] for row in rows]
        assert any(year and year <= 2009 for year in years), "老番段必须有题"
        assert any(year and 2010 <= year <= 2019 for year in years), "中段必须有题"
        assert any(year and year >= 2020 for year in years), "新番段必须有题"

    def test_genre_buckets_all_present(self, quiz_env):
        by_tag = _seed_quiz_variety(quiz_env.session_factory)
        db = quiz_env.session_factory()
        try:
            rows = crud.sample_quiz_contents(db, size=20, seed=2)
        finally:
            db.close()

        ids = {row["id"] for row in rows}
        missing = [
            label
            for label, tag in GENRE_TAGS.items()
            if not ids & set(by_tag.get(tag, []))
        ]
        assert not missing, f"这些口味一张题卡都没有：{missing}"

    def test_falls_back_when_db_is_thin(self, quiz_env):
        """库里只有同一年代的少量作品时不该出不满：用全池兜底，也不抛错。"""
        ids = _seed_contents(quiz_env.session_factory)  # 动画 year 2010~2017
        _tag_all(quiz_env.session_factory, ids)
        db = quiz_env.session_factory()
        try:
            rows = crud.sample_quiz_contents(db, size=20, seed=3)
        finally:
            db.close()

        # 动画只有 8 部、年份全落在中段：老番 / 新番段为空，靠全池兜底出满
        assert len(rows) == 8
        assert {row["type"] for row in rows} == {"anime"}

    def test_same_series_only_appears_once(self, quiz_env):
        """同 IP 多季 / 剧场版不能同时进题面——那是白占两个名额。

        三条去重路径都要覆盖：标题归一键（没归系列的续作）、series_id（归了系列
        但标题看不出来的）、以及中缀标记（series_title_key 只剥尾部，「第三季
        Part.2」「剧场版 Air/真心为你」剥不掉，靠前缀兜住）。
        """
        db = quiz_env.session_factory()
        try:
            db.add_all(
                [
                    Content(
                        title="DedupShow",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99999,
                        image_url="/covers/d1.jpg",
                        year=2015,
                    ),
                    Content(
                        title="DedupShow 第二季",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99998,
                        image_url="/covers/d2.jpg",
                        year=2021,
                    ),
                    Content(
                        title="OtherTitle",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99990,
                        image_url="/covers/o1.jpg",
                        year=2016,
                        series_id=9001,
                    ),
                    Content(
                        title="YetAnotherName",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99980,
                        image_url="/covers/o2.jpg",
                        year=2022,
                        series_id=9001,
                    ),
                    Content(
                        title="MidmarkShow",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99970,
                        image_url="/covers/m1.jpg",
                        year=2013,
                    ),
                    Content(
                        title="MidmarkShow 第三季 Part.2",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99960,
                        image_url="/covers/m2.jpg",
                        year=2019,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        db = quiz_env.session_factory()
        try:
            rows = crud.sample_quiz_contents(db, size=20, seed=5)
        finally:
            db.close()

        titles = [row["title"] for row in rows]
        assert len([t for t in titles if "DedupShow" in t]) == 1
        assert len([t for t in titles if t in ("OtherTitle", "YetAnotherName")]) == 1
        assert len([t for t in titles if "MidmarkShow" in t]) == 1

    def test_prefix_rule_does_not_merge_different_shows(self, quiz_env):
        """前缀判断只认「整串是前缀」：魔法少女小圆 / 魔法少女奈叶必须都留着。"""
        db = quiz_env.session_factory()
        try:
            db.add_all(
                [
                    Content(
                        title="魔法少女小圆",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99999,
                        image_url="/covers/p1.jpg",
                        year=2011,
                    ),
                    Content(
                        title="魔法少女奈叶",
                        type="anime",
                        summary="",
                        rating=8.0,
                        rating_count=99998,
                        image_url="/covers/p2.jpg",
                        year=2004,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        db = quiz_env.session_factory()
        try:
            rows = crud.sample_quiz_contents(db, size=20, seed=6)
        finally:
            db.close()

        titles = {row["title"] for row in rows}
        assert {"魔法少女小圆", "魔法少女奈叶"} <= titles
