"""内容搜索功能测试：JSON API（类型/分页）与作品页搜索交互（表单/空态/翻页）。

原则与 test_api.py 一致：受控临时 SQLite（只含 6 条种子 + 批量造数），
不依赖本地开发库真实数据，不访问外部网络。
首页（/）是门户页，全量浏览与搜索在「全部作品」页（/contents）。
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crud import search_contents, search_contents_page, seed_if_empty
from app.database import Base, get_db
from app.main import app

client = TestClient(app)

BULK_COUNT = 25  # 额外批量作品数量，使分页（每页 20）产生第二页


@pytest.fixture()
def seeded_client(tmp_path):
    """种子库（6 条）+ 批量作品（25 条，无“的”字摘要，避免干扰关键词统计）。"""
    from app.models import Content

    engine = create_engine(
        f"sqlite:///{tmp_path / 'search.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed_if_empty(db)
        for i in range(1, BULK_COUNT + 1):
            db.add(
                Content(
                    title=f"批量作品{i:02d}",
                    type="novel",
                    summary=f"批量测试作品第{i:02d}号，用于翻页验证数据量。",
                    rating=6.0,
                )
            )
        db.commit()

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def empty_client(tmp_path):
    """空库客户端：验证空数据下的页面与 API 行为。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'empty.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


# ---------- 访问层 ----------


def test_crud_search_page_total_and_ids(tmp_path):
    """search_contents_page：总数、id 序与类型/关键词叠加筛选。"""
    from app.database import Base as B
    from app.crud import seed_if_empty as seed

    engine = create_engine(
        f"sqlite:///{tmp_path / 'crud.db'}",
        connect_args={"check_same_thread": False},
    )
    B.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed(db)
        total, items = search_contents_page(db, keyword="乐队")
        assert (total, [c.id for c in items]) == (1, [6])
        total, items = search_contents_page(db, keyword="cyber", content_type="game")
        assert (total, [c.id for c in items]) == (1, [5])
        # 只给类型：按类型浏览；空关键词 + 空类型等价于全量
        total, items = search_contents_page(db, content_type="anime")
        assert total == 2 and [c.id for c in items] == [1, 6]
        total, _ = search_contents_page(db)
        assert total == 6


def test_crud_search_legacy_semantics_kept(tmp_path):
    """既有契约守护：search_contents 不分页、大小写不敏感、按 id 排序。"""
    from app.database import Base as B

    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    B.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed_if_empty(db)
        assert [c.id for c in search_contents(db, "CYBER")] == [5]
        assert [c.id for c in search_contents(db, "乐队")] == [6]
        assert search_contents(db, "绝不存在的词") == []


# ---------- JSON API ----------


def test_api_search_with_type_filter(seeded_client):
    """关键词与类型叠加：交集匹配，类型不符时为空而非报错。"""
    resp = seeded_client.get("/api/contents/search", params={"q": "乐队", "type": "anime"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and [i["id"] for i in body["items"]] == [6]
    assert body["type"] == "anime"

    miss = seeded_client.get("/api/contents/search", params={"q": "乐队", "type": "comic"})
    assert miss.status_code == 200
    assert miss.json()["total"] == 0 and miss.json()["items"] == []


def test_api_search_type_case_insensitive_and_invalid(seeded_client):
    """类型不区分大小写；非法类型返回 400 且错误信息与列表接口一致。"""
    upper = seeded_client.get("/api/contents/search", params={"q": "的", "type": "ANIME"})
    assert upper.status_code == 200
    assert [i["id"] for i in upper.json()["items"]] == [1, 6]

    bad = seeded_client.get("/api/contents/search", params={"q": "的", "type": "unknown"})
    assert bad.status_code == 400
    assert "类型不合法" in bad.json()["detail"]


def test_api_search_pagination(seeded_client):
    """分页回显：total/total_pages/翻页切片按 id 稳定排序；越界页返回空列表。"""
    page1 = seeded_client.get("/api/contents/search", params={"q": "的", "page_size": 2}).json()
    assert (page1["total"], page1["page"], page1["page_size"], page1["total_pages"]) == (6, 1, 2, 3)
    assert [i["id"] for i in page1["items"]] == [1, 2]

    page2 = seeded_client.get(
        "/api/contents/search", params={"q": "的", "page_size": 2, "page": 2}
    ).json()
    assert [i["id"] for i in page2["items"]] == [3, 4]

    beyond = seeded_client.get(
        "/api/contents/search", params={"q": "的", "page_size": 2, "page": 9}
    ).json()
    assert beyond["total"] == 6 and beyond["items"] == []


def test_api_search_blank_q_still_rejected(seeded_client):
    """增量参数不改变原契约：q 为空仍 400（即使带了合法 type）。"""
    resp = seeded_client.get("/api/contents/search", params={"q": "   ", "type": "anime"})
    assert resp.status_code == 400


# ---------- 首页（门户）与全部作品页（/contents）交互 ----------


def test_contents_search_hit_and_feedback(seeded_client):
    """命中：结果统计行（关键词/条数）+ 表单回填 + 命中条目。"""
    resp = seeded_client.get("/contents", params={"q": "星海"})
    assert resp.status_code == 200
    assert "星海航线" in resp.text
    assert "搜索结果" in resp.text and "共 1 条" in resp.text
    assert 'name="q"' in resp.text and 'value="星海"' in resp.text  # 表单与关键词回填
    assert "/contents/1" in resp.text


def test_contents_search_type_filter(seeded_client):
    """仅按类型浏览：只显示该类型条目，统计行带类型中文标签。"""
    resp = seeded_client.get("/contents", params={"type": "game"})
    assert resp.status_code == 200
    assert "迷宫回响" in resp.text and "Cyber Shrine" in resp.text
    assert "星海航线" not in resp.text
    assert "类型：游戏" in resp.text and "共 2 条" in resp.text


def test_contents_search_empty_state(seeded_client):
    """无匹配：空态 + 清除筛选入口（指向作品页自身），而非空白网格。"""
    resp = seeded_client.get("/contents", params={"q": "绝不存在的关键词xyz"})
    assert resp.status_code == 200
    assert "没有找到匹配的内容" in resp.text
    assert 'href="/contents"' in resp.text  # 清除筛选链接


def test_contents_search_pagination_links(seeded_client):
    """翻页：第二页有“上一页”且只含后半段数据；第一页有“下一页”。"""
    page1 = seeded_client.get("/contents", params={"q": "批量"})
    assert "共 25 条" in page1.text and "下一页" in page1.text
    assert "批量作品01" in page1.text

    page2 = seeded_client.get("/contents", params={"q": "批量", "page": 2})
    assert "上一页" in page2.text
    assert "批量作品21" in page2.text and "批量作品01" not in page2.text
    # 末页：「下一页」渲染为禁用态（复用 _pager.html 分页宏，禁用项不可点）
    assert re.search(
        r'<li class="page-item disabled">\s*<a class="page-link"[^>]*>下一页</a>', page2.text
    )


def test_index_is_portal_without_full_list(seeded_client):
    """无筛选参数：首页是门户（分类/推荐/资讯），不再渲染全量列表。"""
    resp = seeded_client.get("/")
    assert resp.status_code == 200
    assert "搜索结果" not in resp.text
    assert "内容列表" not in resp.text  # 全量列表已移到「全部作品」页
    assert "分类浏览" in resp.text and "推荐作品" in resp.text
    assert "/contents/1" in resp.text and "星海航线" in resp.text  # 推荐位仍链到详情


def test_index_ignores_filter_params(seeded_client):
    """首页不再接受筛选参数（q/type 归作品页）：带参访问仍是门户，不报错。"""
    resp = seeded_client.get("/", params={"type": "banana", "q": "星海"})
    assert resp.status_code == 200
    assert "搜索结果" not in resp.text
    assert "星海航线" in resp.text  # 推荐位


def _client_with_works(tmp_path, works, filename):
    """用给定 (标题, 评分, 标签) 造一个小库并返回客户端。"""
    from app import crud
    from app.models import Content

    engine = create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        for title, rating, tags in works:
            row = Content(title=title, type="anime", summary=f"{title}简介", rating=rating)
            db.add(row)
            db.commit()
            crud.replace_content_tags(db, row.id, tags)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    return client, factory


def test_index_recommends_by_home_taste(tmp_path):
    """首页推荐作品按站内口味挑：本命命中优先、避雷一票否决、无命中不凑数。"""
    client, factory = _client_with_works(
        tmp_path,
        [
            ("双命中贴贴番", 8.0, ["日常", "百合"]),
            ("双命中贴贴番", 7.6, ["日常", "百合"]),  # 同名不同条目：门户只占一个位
            ("单命中轻喜剧", 8.9, ["日常"]),
            ("致郁高分雷", 9.9, ["日常", "致郁"]),   # 高分带避雷：一票否决
            ("科幻无关番", 9.5, ["科幻"]),            # 无本命命中：不进推荐位
        ],
        "taste.db",
    )
    try:
        html = client.get("/").text
        assert "单命中轻喜剧" in html
        assert "致郁高分雷" not in html
        assert "科幻无关番" not in html
        titles = re.findall(r'work-card-title">([^<]*)', html)
        assert titles.count("双命中贴贴番") == 1          # 同名去重
        # 命中数优先于评分：8.0 的双命中排在 8.9 的单命中之前
        assert titles.index("双命中贴贴番") < titles.index("单命中轻喜剧")
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_index_falls_back_to_rating_without_taste_match(tmp_path):
    """一个本命命中都没有时回落评分榜，门户不空。"""
    client, factory = _client_with_works(
        tmp_path,
        [("硬核机战甲", 9.2, ["机战", "战斗"]), ("科幻史诗乙", 8.8, ["科幻"])],
        "fallback.db",
    )
    try:
        html = client.get("/").text
        assert "硬核机战甲" in html and "科幻史诗乙" in html
        assert "暂无内容数据" not in html
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_index_recommends_one_per_series(tmp_path):
    """同系列多季只占一个推荐位，且取最早的一季（从头看起）。"""
    client, factory = _client_with_works(
        tmp_path,
        [
            ("街角魔族", 8.0, ["日常", "百合"]),
            ("街角魔族 2丁目", 8.9, ["日常", "百合", "搞笑"]),   # 同系列第二季，命中更多
            ("摇曳百合", 8.2, ["日常", "百合"]),
            ("摇曳百合 夏日时光！", 7.6, ["日常", "百合"]),      # 尾缀 + 标点不同
            ("别当欧尼酱了！", 7.5, ["日常", "搞笑"]),
        ],
        "series.db",
    )
    try:
        titles = re.findall(r'work-card-title">([^<]*)', client.get("/").text)
        assert titles.count("街角魔族") == 1 and "街角魔族 2丁目" not in titles
        assert titles.count("摇曳百合") == 1 and "摇曳百合 夏日时光！" not in titles
        assert "别当欧尼酱了！" in titles
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_empty_database_portal_and_works_search(empty_client):
    """空库：首页门户展示推荐空态；作品页搜索提示无匹配，两者都保持 200。"""
    plain = empty_client.get("/")
    assert plain.status_code == 200
    assert "暂无内容数据" in plain.text and 'name="q"' in plain.text

    searched = empty_client.get("/contents", params={"q": "任意"})
    assert searched.status_code == 200
    assert "没有找到匹配的内容" in searched.text
