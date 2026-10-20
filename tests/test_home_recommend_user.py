"""首页推荐作品（登录用户）：站内收藏/标注口味、总/近期双象、画像双闸切换。

覆盖：
- 登录无任何信号 → 回落站长口味（与游客一致）；
- 有收藏/标注 → 按口味推荐，已收藏/标注不进推荐位；
- 取消收藏但留标注仍是口味证据（行存在即有关系）；
- 总口味/近期口味双象：近期命中的题材排名高于长期口味；
- 成分检测画像未超「比例+基数」双闸 → 画像口径；超闸 → 实时口径；
- 实时口径继承画像避雷标签（站内无负向信号，雷区不丢）；
- 浏览记录不进算法。
"""

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Content, ContentFavorite
from auth_helpers import USER_USERNAME, create_accounts, login_as_user


@pytest.fixture
def home_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'home_user.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
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


def _add_works(session_factory, works):
    """造作品（标题, 评分, 标签），返回 {标题: content_id}。"""
    ids: dict[str, int] = {}
    db = session_factory()
    try:
        for title, rating, tags in works:
            row = Content(
                title=title, type="anime", summary=f"{title}简介", rating=rating
            )
            db.add(row)
            db.commit()
            crud.replace_content_tags(db, row.id, tags)
            ids[title] = row.id
    finally:
        db.close()
    return ids


def _relate(session_factory, content_ids, *, status=None, days_ago=None):
    """给测试账号建收藏/标注关系；days_ago 控制活动时间（造「近期」用）。"""
    uid = _uid(session_factory)
    created = datetime.now() - timedelta(days=days_ago or 0)
    db = session_factory()
    try:
        for content_id in content_ids:
            db.add(
                ContentFavorite(
                    user_id=uid,
                    content_id=content_id,
                    favorited=True,
                    status=status,
                    status_updated_at=created if status else None,
                    created_at=created,
                )
            )
        db.commit()
    finally:
        db.close()


def _save_profile(session_factory, profile: dict) -> None:
    uid = _uid(session_factory)
    db = session_factory()
    try:
        crud.save_persona_report(
            db,
            uid,
            bangumi_username="tester",
            tone="sharp",
            payload={"profile": profile},
        )
    finally:
        db.close()


def _titles(html: str) -> list[str]:
    return re.findall(r'work-card-title">([^<]*)', html)


def test_logged_in_without_signals_falls_back_to_editor_taste(home_env):
    """登录但没收藏/标注/画像：推荐位与游客同一套站长口味，不会空。"""
    _add_works(
        home_env.session_factory,
        [
            ("双命中贴贴番", 8.0, ["日常", "百合"]),
            ("致郁高分雷", 9.9, ["日常", "致郁"]),
            ("科幻无关番", 9.5, ["科幻"]),
        ],
    )
    html = home_env.client.get("/").text
    assert "双命中贴贴番" in html
    assert "致郁高分雷" not in html
    assert "科幻无关番" not in html


def test_logged_in_recommends_by_favorites_and_excludes_related(home_env):
    """有收藏/标注：按口味推荐；已收藏/标注的作品不占推荐位。"""
    ids = _add_works(
        home_env.session_factory,
        [
            ("看过的机战番", 7.0, ["机战", "战斗"]),
            ("没标记的机战番", 8.5, ["机战"]),
            ("双命机战番", 9.0, ["机战", "战斗"]),
            ("无关日常番", 9.9, ["日常", "百合"]),
        ],
    )
    _relate(home_env.session_factory, [ids["看过的机战番"]], status="seen")
    titles = _titles(home_env.client.get("/").text)
    assert "双命机战番" in titles and "没标记的机战番" in titles
    assert "看过的机战番" not in titles  # 已收藏：不重复推荐
    assert "无关日常番" not in titles  # 没命中用户口味，不拿高分稀释


def test_annotation_without_favorite_still_counts(home_env):
    """取消收藏但留标注的作品仍是口味证据（行存在即有关系）。"""
    factory = home_env.session_factory
    ids = _add_works(
        factory,
        [
            ("标注过的机战番", 8.0, ["机战"]),
            ("另一部机战番", 8.5, ["机战"]),
            ("无关番", 9.9, ["日常"]),
        ],
    )
    _relate(factory, [ids["标注过的机战番"]], status="want")
    db = factory()
    try:
        db.execute(
            update(ContentFavorite)
            .where(
                ContentFavorite.user_id == _uid(factory),
                ContentFavorite.content_id == ids["标注过的机战番"],
            )
            .values(favorited=False)
        )
        db.commit()
    finally:
        db.close()
    titles = _titles(home_env.client.get("/").text)
    assert "另一部机战番" in titles  # 标注（想看看过）仍定义口味
    assert "标注过的机战番" not in titles  # 有关系的作品不重复推荐
    assert "无关番" not in titles


def test_recent_taste_outranks_total_taste(home_env):
    """总/近期双象：近期沉迷的题材排名高于长期口味（近期权重 2 倍）。"""
    factory = home_env.session_factory
    old = _add_works(factory, [(f"老日常番{i}", 9.0, ["日常"]) for i in range(22)])
    new = _add_works(factory, [(f"新百合番{i}", 9.0, ["百合"]) for i in range(3)])
    _add_works(
        factory,
        [
            ("候选旧日常番", 9.5, ["日常"]),
            ("候选新百合番", 7.0, ["百合"]),
        ],
    )
    _relate(factory, list(old.values()), days_ago=200)  # 超出 90 天窗：只进总口味
    _relate(factory, list(new.values()), days_ago=1)  # 窗内：进近期口味
    titles = _titles(home_env.client.get("/").text)
    assert titles.index("候选新百合番") < titles.index("候选旧日常番")


def test_profile_taste_used_until_double_threshold(home_env):
    """画像口径 vs 实时口径：站内收藏数要过「比例+基数」双闸才切换。"""
    factory = home_env.session_factory
    works = [
        ("科幻候选番", 7.0, ["科幻"]),
        ("日常候选番", 9.9, ["日常"]),
    ] + [(f"老收藏{i}", 8.0, ["日常"]) for i in range(14)]
    ids = _add_works(factory, works)
    _save_profile(
        factory,
        {
            "total": 4,
            "top_genres": [{"name": "科幻", "count": 3}, {"name": "机战", "count": 1}],
            "avoided_tags": [],
        },
    )
    _relate(factory, [ids[f"老收藏{i}"] for i in range(5)], days_ago=100)

    # 未超闸（5 < max(4×1.5, 4+10)=14）：画像口径——推科幻，不推日常
    titles = _titles(home_env.client.get("/").text)
    assert "科幻候选番" in titles and "日常候选番" not in titles

    # 再收藏 9 部（累计 14，同时满足 ≥6 且 ≥14）：切实时口径，日常翻身
    _relate(factory, [ids[f"老收藏{i}"] for i in range(5, 14)], days_ago=0)
    titles = _titles(home_env.client.get("/").text)
    assert "日常候选番" in titles
    assert titles.index("日常候选番") < titles.index("科幻候选番")


def test_avoided_tags_inherited_from_profile_in_live_mode(home_env):
    """实时口径继承画像避雷：站内没有抛弃/低分信号，跑过检测的用户的雷区不丢。"""
    factory = home_env.session_factory
    works = [
        ("致郁日常番", 9.9, ["日常", "致郁"]),
        ("纯日常番", 8.0, ["日常"]),
    ] + [(f"收藏{i}", 8.0, ["日常"]) for i in range(14)]
    ids = _add_works(factory, works)
    _save_profile(
        factory,
        {
            "total": 4,
            "top_genres": [{"name": "治愈", "count": 2}],
            "avoided_tags": [{"name": "致郁", "count": 2}],
        },
    )
    _relate(factory, [ids[f"收藏{i}"] for i in range(14)], days_ago=0)
    titles = _titles(home_env.client.get("/").text)
    assert "纯日常番" in titles
    assert "致郁日常番" not in titles  # 避雷一票否决（继承自画像）


def test_browse_history_not_used_for_recommendation(home_env):
    """浏览记录不进算法：只看过没收藏的用户吃站长口味，不被浏览带偏。"""
    factory = home_env.session_factory
    uid = _uid(factory)
    ids = _add_works(
        factory,
        [
            ("机战浏览番", 9.9, ["机战"]),
            ("贴贴口味番", 7.0, ["日常", "百合"]),
        ],
    )
    db = factory()
    try:
        crud.record_content_view(db, uid, ids["机战浏览番"])
        crud.record_content_view(db, uid, ids["机战浏览番"])
    finally:
        db.close()
    titles = _titles(home_env.client.get("/").text)
    assert "贴贴口味番" in titles  # 站长口味命中
    assert "机战浏览番" not in titles  # 浏览不计入口味


def test_logged_in_module_expands_to_max(home_env):
    """登录路径同样铺满模块：101 部库 - 1 部已收藏 = 100 部推荐，40 部收起。"""
    factory = home_env.session_factory
    ids = _add_works(
        factory,
        [(f"机战番{i:03d}", 9.0 - i * 0.01, ["机战"]) for i in range(101)],
    )
    _relate(factory, [ids["机战番000"]], status="seen")
    html = home_env.client.get("/").text
    titles = _titles(html)
    assert len(titles) == 100                       # 101 - 1 部已收藏
    assert titles[0] == "机战番001"                  # 已收藏的 000 不占推荐位
    assert html.count('class="work-card home-top-extra"') == 40  # 100 - 60：收起态
    assert "还有 40 部" in html                      # 展开按钮
