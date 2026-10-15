"""内容搜索功能测试：JSON API（类型/分页）与首页搜索交互（表单/空态/翻页）。

原则与 test_api.py 一致：受控临时 SQLite（只含 6 条种子 + 批量造数），
不依赖本地开发库真实数据，不访问外部网络。
"""

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
    """第一阶段契约守护：search_contents 不分页、大小写不敏感、按 id 排序。"""
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


# ---------- 首页搜索交互 ----------


def test_index_search_hit_and_feedback(seeded_client):
    """命中：结果统计行（关键词/条数）+ 表单回填 + 命中条目。"""
    resp = seeded_client.get("/", params={"q": "星海"})
    assert resp.status_code == 200
    assert "星海航线" in resp.text
    assert "搜索结果" in resp.text and "共 1 条" in resp.text
    assert 'name="q"' in resp.text and 'value="星海"' in resp.text  # 表单与关键词回填
    assert "/contents/1" in resp.text


def test_index_search_type_filter(seeded_client):
    """仅按类型浏览：只显示该类型条目，统计行带类型中文标签。"""
    resp = seeded_client.get("/", params={"type": "game"})
    assert resp.status_code == 200
    assert "迷宫回响" in resp.text and "Cyber Shrine" in resp.text
    assert "星海航线" not in resp.text
    assert "类型：游戏" in resp.text and "共 2 条" in resp.text


def test_index_search_empty_state(seeded_client):
    """无匹配：警示空态 + 清除筛选入口，而非空白表格。"""
    resp = seeded_client.get("/", params={"q": "绝不存在的关键词xyz"})
    assert resp.status_code == 200
    assert "没有找到匹配的内容" in resp.text
    assert 'href="/"' in resp.text  # 清除筛选链接


def test_index_search_pagination_links(seeded_client):
    """翻页：第二页有“上一页”且只含后半段数据；第一页有“下一页”。"""
    page1 = seeded_client.get("/", params={"q": "批量"})
    assert "共 25 条" in page1.text and "下一页" in page1.text
    assert "批量作品01" in page1.text

    page2 = seeded_client.get("/", params={"q": "批量", "page": 2})
    assert "上一页" in page2.text and "下一页" not in page2.text
    assert "批量作品21" in page2.text and "批量作品01" not in page2.text


def test_index_default_view_without_search(seeded_client):
    """无筛选参数时保持原样：全量列表，无搜索统计行与分页。"""
    resp = seeded_client.get("/")
    assert resp.status_code == 200
    assert "搜索结果" not in resp.text
    assert "/contents/1" in resp.text and "星海航线" in resp.text


def test_index_invalid_type_falls_back_to_full_list(seeded_client):
    """页面侧宽容处理：非法类型被忽略，回退全量列表（严格校验归 API）。"""
    resp = seeded_client.get("/", params={"type": "banana"})
    assert resp.status_code == 200
    assert "搜索结果" not in resp.text
    assert "星海航线" in resp.text


def test_index_empty_database_with_and_without_search(empty_client):
    """空库：默认视图提示暂无数据；搜索态提示无匹配，两者都保持 200。"""
    plain = empty_client.get("/")
    assert plain.status_code == 200
    assert "暂无内容数据" in plain.text and 'name="q"' in plain.text

    searched = empty_client.get("/", params={"q": "任意"})
    assert searched.status_code == 200
    assert "没有找到匹配的内容" in searched.text
