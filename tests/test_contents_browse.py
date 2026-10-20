"""全部作品页改版测试（2026-10-20）：形式载体归类、排序三档与页面新分区。

覆盖四件事：
1. tagging 层：形式载体标签（TV / 剧场版 / 漫画改…）被识别为 media / source
   而不是噪音，并能由清洗脚本搬进 contents 列；
2. crud 层：形式两列的计数查询、rating / favorites / newest 三档排序，
   以及收藏人数批量计数（B站卡片元数据行用）；
3. /contents 页面：hero 品牌带、吸顶类型 Tab、形式 chips、排序 chips、
   已选条件条、B站卡片（默认视图）/ 网格 / 列表三视图，
   以及「新参数未选时不写入 URL」这条硬约束（既有分享链接形状不能变）；
4. B站卡片元数据行随排序自适应：评分/收藏/年份哪个排序哪个前置加粗。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

from datetime import datetime

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Content, ContentFavorite, User
from app.tagging import (
    classify_tag,
    form_tag_names,
    is_form_tag,
    media_of,
    source_material_of,
    split_tags_full,
)


@pytest.fixture
def browse_env(tmp_path):
    """独立临时库：三部动画（不同评分 / 年份 / 形式）+ 两个用户（收藏数不同）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'browse.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        for title, rating, year, media, source in (
            ("高分新番甲", 9.2, 2025, "TV", "漫画改"),
            ("低分老番乙", 5.1, 2015, "剧场版", "原创"),
            ("中间年份丙", 7.0, 2020, "TV", "小说改"),
        ):
            db.add(
                Content(
                    title=title,
                    type="anime",
                    summary=f"{title} 的简介。",
                    rating=rating,
                    year=year,
                    media=media,
                    source_material=source,
                )
            )
        db.commit()
        crud.replace_content_tags(db, 1, ["搞笑", "TV", "漫画改"])
        crud.replace_content_tags(db, 2, ["治愈", "剧场版", "原创"])
        crud.replace_content_tags(db, 3, ["悬疑", "TV", "小说改"])
        # 两个用户收藏「中间年份丙」、一个用户收藏「高分新番甲」：
        # 收藏排序预期 丙(2) > 甲(1) > 乙(0)
        for username in ("u1", "u2"):
            db.add(User(username=username, password_hash="x", role="user"))
        db.commit()
        db.add(ContentFavorite(user_id=1, content_id=3, favorited=True))
        db.add(ContentFavorite(user_id=2, content_id=3, favorited=True))
        db.add(ContentFavorite(user_id=1, content_id=1, favorited=True))
        # 未收藏作品（favorited=False）不计入收藏排序
        db.add(ContentFavorite(user_id=2, content_id=2, favorited=False))
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            client=TestClient(app, follow_redirects=False),
            session_factory=TestingSession,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------- tagging：形式载体从噪音里被识别出来 ----------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("TV", "TV"),
        ("tv", "TV"),
        ("TV动画", "TV"),
        ("剧场版", "剧场版"),
        ("映画", "剧场版"),
        ("OVA", "OVA"),
        ("WEB动画", "WEB"),
    ],
)
def test_media_of_normalizes_carrier_tags(tag, expected):
    assert media_of(tag) == expected
    assert classify_tag(tag) == ("media", expected)


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("漫画改", "漫画改"),
        ("漫改", "漫画改"),
        ("小说改", "小说改"),
        ("轻改", "小说改"),
        ("游戏改", "游戏改"),
        ("原创动画", "原创"),
    ],
)
def test_source_material_of_normalizes_tags(tag, expected):
    assert source_material_of(tag) == expected
    assert classify_tag(tag) == ("source", expected)


def test_is_form_tag_separates_form_from_genre():
    assert is_form_tag("TV") and is_form_tag("漫画改")
    assert not is_form_tag("搞笑")  # 题材不是形式
    assert not is_form_tag("2011")  # 年份不是形式


def test_split_tags_full_carries_form_values():
    genres, studios, year, media, source = split_tags_full(
        ["搞笑", "TV", "漫画改", "A-1 Pictures", "2011"]
    )
    assert genres == ["搞笑"]
    assert studios == ["A-1 Pictures"]
    assert year == 2011
    assert media == "TV"
    assert source == "漫画改"


def test_split_tags_keeps_three_tuple_contract():
    """历史调用方只认三元组（题材 / 公司 / 年份），形式载体另走 full 版。"""
    assert split_tags_full(["搞笑", "TV"])[:3] == (["搞笑"], [], None)


# ---------- crud：形式计数与三档排序 ----------


def test_form_counts_come_from_columns(browse_env):
    db = browse_env.session_factory()
    try:
        assert crud.list_media_with_counts(db, "anime") == [("TV", 2), ("剧场版", 1)]
        assert crud.list_source_material_with_counts(db, "anime") == [
            ("原创", 1),
            ("小说改", 1),
            ("漫画改", 1),
        ]
        # 类型隔离：漫画库里没有这些形式
        assert crud.list_media_with_counts(db, "comic") == []
    finally:
        db.close()


def test_form_filter_narrows_results(browse_env):
    db = browse_env.session_factory()
    try:
        total, items = crud.search_contents_page(db, content_type="anime", media="TV")
        assert total == 2
        assert {item.title for item in items} == {"高分新番甲", "中间年份丙"}

        total, items = crud.search_contents_page(
            db, content_type="anime", media="TV", source_material="小说改"
        )
        assert [item.title for item in items] == ["中间年份丙"]
    finally:
        db.close()


def test_sort_rating(browse_env):
    db = browse_env.session_factory()
    try:
        _, items = crud.search_contents_page(db, content_type="anime", sort="rating")
        assert [item.title for item in items] == ["高分新番甲", "中间年份丙", "低分老番乙"]
    finally:
        db.close()


def test_sort_newest(browse_env):
    db = browse_env.session_factory()
    try:
        _, items = crud.search_contents_page(db, content_type="anime", sort="newest")
        assert [item.title for item in items] == ["高分新番甲", "中间年份丙", "低分老番乙"]
    finally:
        db.close()


def test_sort_favorites_ignores_unfavorited(browse_env):
    """收藏排序只数 favorited=True 的行；没被收藏的作品排最后（按评分兜底）。"""
    db = browse_env.session_factory()
    try:
        _, items = crud.search_contents_page(db, content_type="anime", sort="favorites")
        assert [item.title for item in items] == ["中间年份丙", "高分新番甲", "低分老番乙"]
    finally:
        db.close()


def test_sort_unknown_value_falls_back_to_default(browse_env):
    db = browse_env.session_factory()
    try:
        _, items = crud.search_contents_page(db, content_type="anime", sort="nonsense")
        assert [item.title for item in items] == ["高分新番甲", "低分老番乙", "中间年份丙"]
    finally:
        db.close()


def test_genre_chips_exclude_form_tags(browse_env):
    """题材组不该出现 TV / 漫画改 —— 它们已由形式 chips 承载。

    排除必须发生在 SQL 层（limit 之前）：否则高频形式词会把题材名额吃光。
    """
    db = browse_env.session_factory()
    try:
        names = [name for name, _ in crud.list_tags_with_counts(db, content_type="anime")]
        # 默认口径（历史行为）不做排除，形式词照样在库里
        assert "TV" in names and "漫画改" in names
        names = [
            name
            for name, _ in crud.list_tags_with_counts(
                db, content_type="anime", exclude=form_tag_names()
            )
        ]
        assert set(names) == {"搞笑", "治愈", "悬疑"}
    finally:
        db.close()


# ---------- 清洗脚本：形式标签搬进列而不是丢掉 ----------


def test_clean_moves_form_tags_into_columns(tmp_path):
    """清洗的核心改动：TV / 漫画改 从 content_tags 搬进 contents.media /
    source_material。旧脚本会把它们当噪音丢弃，形式筛选就没数据了。"""
    from scripts.clean_content_tags import clean_contents

    engine = create_engine(
        f"sqlite:///{tmp_path / 'clean.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        work = Content(
            title="清洗对象", type="anime", summary="简介。", rating=7.0, bangumi_id=12345
        )
        db.add(work)
        db.commit()
        crud.replace_content_tags(db, work.id, ["搞笑", "TV", "漫画改", "2011"])

        summary = clean_contents(db, apply=True)
        db.refresh(work)

        assert summary.applied == 1
        assert crud.list_content_tags(db, work.id) == ["搞笑"]  # 题材留下
        assert work.media == "TV"
        assert work.source_material == "漫画改"
        assert work.year == 2011
        assert summary.media_set == 1 and summary.source_set == 1
    finally:
        db.close()


def test_clean_form_tags_override_existing_values(tmp_path):
    """content_tags 是形式两列的真相来源：标签里有形式词就覆盖任何既有值。

    这条判据在 2026-10-20 上午的会话里从「列已有值不覆盖」改成「标签推导」——
    旧逻辑会把历史脏值（游戏 / 其他 / 2025 混进 media / source_material）
    原样留在列里，形式筛选组跟着显示垃圾。
    """
    from scripts.clean_content_tags import clean_contents

    engine = create_engine(
        f"sqlite:///{tmp_path / 'clean2.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        work = Content(
            title="已有形式值",
            type="anime",
            summary="简介。",
            rating=7.0,
            media="剧场版",
            source_material="游戏",
        )
        db.add(work)
        db.commit()
        crud.replace_content_tags(db, work.id, ["TV"])

        clean_contents(db, apply=True)
        db.refresh(work)

        assert work.media == "TV"  # 标签里的形式词覆盖既有值
        assert work.source_material is None  # 无原作词 + 既有值是脏数据 → 清掉
        assert crud.list_content_tags(db, work.id) == []
    finally:
        db.close()


def test_clean_keeps_valid_form_values_without_tags(tmp_path):
    """标签里没有形式词时，列里已有的合法枚举值保留（详情页 / 人工补的优先）。"""
    from scripts.clean_content_tags import clean_contents

    engine = create_engine(
        f"sqlite:///{tmp_path / 'clean3.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        work = Content(
            title="无形式词",
            type="anime",
            summary="简介。",
            rating=7.0,
            media="OVA",
            source_material="原创",
        )
        db.add(work)
        db.commit()
        crud.replace_content_tags(db, work.id, ["搞笑"])

        clean_contents(db, apply=True)
        db.refresh(work)

        assert work.media == "OVA"  # 合法枚举值原样保留
        assert work.source_material == "原创"
    finally:
        db.close()


def test_clean_form_columns_are_idempotent(tmp_path):
    """幂等：连跑两次第二次没有任何变更（合法值不会被反复清空）。"""
    from scripts.clean_content_tags import clean_contents

    engine = create_engine(
        f"sqlite:///{tmp_path / 'clean4.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        work = Content(title="幂等对象", type="anime", summary="简介。", rating=7.0)
        db.add(work)
        db.commit()
        crud.replace_content_tags(db, work.id, ["搞笑", "剧场版", "小说改"])

        first = clean_contents(db, apply=True)
        db.refresh(work)
        assert first.applied == 1
        assert work.media == "剧场版" and work.source_material == "小说改"

        second = clean_contents(db, apply=True)
        db.refresh(work)
        assert second.changed == 0  # 第二次扫描零变更
        assert work.media == "剧场版" and work.source_material == "小说改"
    finally:
        db.close()


# ---------- /contents 页面：新分区与 URL 约束 ----------


def test_type_tabs_rendered_with_counts(browse_env):
    resp = browse_env.client.get("/contents")
    assert resp.status_code == 200
    assert 'class="type-tabs' in resp.text
    assert 'class="type-tab active"' in resp.text  # 未选类型时「全部」高亮
    assert "全部" in resp.text and "动画" in resp.text
    # 全部 + 四个类型共 5 个 Tab，每个都带真实作品数
    assert resp.text.count('class="type-tab-count"') == 5


def test_form_chips_rendered_from_columns(browse_env):
    resp = browse_env.client.get("/contents?type=anime")
    assert resp.status_code == 200
    assert ">形式<" in resp.text
    assert "TV" in resp.text and "剧场版" in resp.text and "漫画改" in resp.text
    # 形式 chips 的参数是 media / source，不再是 tags
    assert "media=TV" in resp.text or "media=TV" in resp.text


def test_sort_chips_rendered(browse_env):
    resp = browse_env.client.get("/contents")
    assert resp.status_code == 200
    for label in ("评分最高", "收藏最多", "时间最新"):
        assert label in resp.text
    assert "sort=rating" in resp.text and "sort=favorites" in resp.text
    assert "sort=newest" in resp.text


def test_sort_applies_and_shows_in_summary(browse_env):
    resp = browse_env.client.get("/contents?type=anime&sort=newest")
    assert resp.status_code == 200
    assert "按时间最新" in resp.text
    assert resp.text.index("高分新番甲") < resp.text.index("低分老番乙")


def test_active_filter_bar_shows_selected_conditions(browse_env):
    resp = browse_env.client.get("/contents?type=anime&tags=%E6%90%9E%E7%AC%91&sort=rating")
    assert resp.status_code == 200
    assert 'class="active-filters' in resp.text
    assert "清除全部" in resp.text


def test_list_view_and_view_switch(browse_env):
    resp = browse_env.client.get("/contents?view=list")
    assert resp.status_code == 200
    assert 'class="works-list"' in resp.text
    assert 'class="work-row"' in resp.text
    # 视图切换链接：卡片（默认）不写参数，网格写 view=grid，列表写 view=list
    assert 'href="/contents?q=&amp;type=&amp;tags=&amp;creator=&amp;view=list"' in resp.text
    assert 'href="/contents?q=&amp;type=&amp;tags=&amp;creator=&amp;view=grid"' in resp.text

    # 默认页 = B站卡片视图（封面 + 标题 + 元数据行），不再是旧网格
    default = browse_env.client.get("/contents")
    assert 'class="works-grid works-grid--bili"' in default.text
    assert 'class="bili-card"' in default.text

    grid = browse_env.client.get("/contents?view=grid")
    assert 'class="works-grid"' in grid.text  # 信息模式：带摘要
    assert "work-card-summary" in grid.text


def test_new_params_absent_when_unselected(browse_env):
    """未选的新参数不写入 URL：既有分享链接与断言都按旧形状书写。

    注意 chips 链接必然带 media= —— 它们的作用就是「选中该形式」，
    这里守的是「当前没选任何新条件时，导航类链接（类型 Tab / 清除）保持旧形状」。
    """
    resp = browse_env.client.get("/contents")
    assert resp.status_code == 200
    assert 'href="/contents?q=&amp;type=anime&amp;tags=&amp;creator="' in resp.text


def test_pager_preserves_sort_and_view(browse_env):
    resp = browse_env.client.get("/contents?sort=rating&view=list&tags=%E6%90%9E%E7%AC%91")
    assert resp.status_code == 200
    assert "sort=rating" in resp.text
    assert "view=list" in resp.text


# ---------- B站卡片（默认视图）：hero / 吸顶 / 元数据行自适应 ----------


def test_favorite_counts_batch_query(browse_env):
    """收藏人数批量计数：只数 favorited=True，未收藏的作品不出现在结果里。"""
    db = browse_env.session_factory()
    try:
        counts = crud.list_favorite_counts(db, [1, 2, 3])
        assert counts == {1: 1, 3: 2}  # 甲 1、丙 2；乙只被取消过收藏，不计
        assert crud.list_favorite_counts(db, []) == {}
    finally:
        db.close()


def test_default_view_is_bili_cards(browse_env):
    """默认视图抄哔哩哔哩：封面 + 标题 + 元数据行；未选 view 时不写 URL。"""
    resp = browse_env.client.get("/contents")
    assert resp.status_code == 200
    assert 'class="works-grid works-grid--bili"' in resp.text
    assert 'class="bili-card"' in resp.text
    assert 'class="bili-title"' in resp.text
    assert 'class="bili-meta"' in resp.text
    # 无排序回退默认序：评分前置加粗
    assert 'class="bili-rating bili-strong">★' in resp.text
    # 卡片链接形状不变（新参数未选不写入）
    assert 'href="/contents?q=&amp;type=&amp;tags=&amp;creator="' in resp.text


def test_hero_band_renders_with_real_stats(browse_env):
    """hero 品牌带：字标 + 既有标题文案 + 真实库统计（总数 + 四类型）。"""
    resp = browse_env.client.get("/contents")
    assert 'class="contents-hero"' in resp.text
    assert 'class="contents-hero-bg"' in resp.text  # 本地主视觉素材层
    assert 'class="contents-hero-title">全部作品<' in resp.text
    # 既有描述文案不可改（品牌口径 + 测试依赖）
    assert "库内全部 ACGN 作品，支持按类型、题材、形式与年份筛选。" in resp.text
    # 统计：3 部作品 + 动画 3（测试库只有动画）
    assert "<dt>收录作品</dt>" in resp.text and "<dd>3</dd>" in resp.text


def test_sticky_bar_holds_tabs_and_view_switch(browse_env):
    """吸顶条：类型 Tab（图标 + 四色 data-type）+ 三枚视图切换都在里面。"""
    resp = browse_env.client.get("/contents")
    assert 'class="works-sticky"' in resp.text
    assert 'class="type-tab-icon"' in resp.text
    assert 'data-type="anime"' in resp.text
    for label in ("卡片", "网格", "列表"):
        assert f">{label}</a>" in resp.text
    # 吸顶条的 Tab 仍带真实计数（与 hero 统计同源）
    assert resp.text.count('class="type-tab-count"') == 5


def test_meta_row_adapts_to_sort(browse_env):
    """元数据行随排序自适应：收藏排序 ♥ 前置、最新排序年份前置。"""
    fav = browse_env.client.get("/contents?type=anime&sort=favorites")
    assert 'class="bili-fav bili-strong">♥' in fav.text
    assert 'class="bili-rating bili-strong"' not in fav.text

    new = browse_env.client.get("/contents?type=anime&sort=newest")
    assert 'class="bili-year bili-strong">' in new.text
    assert 'class="bili-fav bili-strong"' not in new.text

    # 收藏最多排序下，卡片 ♥ 计数与收藏排序同源（丙 2 > 甲 1 > 乙 0）
    assert "♥ 2" in fav.text and "♥ 1" in fav.text


def test_current_year_badge_on_cards(browse_env):
    """当年作品封面带「新」角标；非当年作品没有。"""
    db = browse_env.session_factory()
    try:
        db.add(
            Content(
                title="当年新番丁",
                type="anime",
                summary="简介。",
                rating=8.0,
                year=datetime.now().year,
                media="TV",
                source_material="原创",
            )
        )
        db.commit()
    finally:
        db.close()

    resp = browse_env.client.get("/contents?type=anime&sort=newest")
    assert resp.text.count('class="bili-new"') == 1  # 只有当年那部
    assert "当年新番丁" in resp.text


def test_empty_state_illustration(browse_env):
    """无结果空态：插图 + 既有文案 + 放宽条件入口。"""
    resp = browse_env.client.get("/contents?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E5%85%B3%E9%94%AE%E8%AF%8D")
    assert resp.status_code == 200
    assert 'class="detail-empty detail-empty--illustration"' in resp.text
    assert 'src="/static/img/contents/contents-empty.png"' in resp.text
    assert "没有找到匹配的内容。试试更短的关键词" in resp.text
    assert 'class="bili-card"' not in resp.text  # 空态不渲染卡片
