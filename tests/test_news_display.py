"""资讯展示层回归：首页软配额 feed、官方 PR 折叠、成人内容全局显示设置。

覆盖验收要点 5（首页 30 条软配额 + 类别覆盖）与 6（成人开关只隐藏图片与露骨摘要，
不误隐藏正常资讯、不等于年龄验证）。依赖覆盖 + 独立临时 SQLite，不触网。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Article

ADULT_TITLE = "18禁ギャルゲー新作発売決定"
NORMAL_TITLE = "新作アニメ制作決定、2027年放送"


@pytest.fixture
def news_client(tmp_path):
    """独立临时库：一条成人向 galge + 一条正常动画 + 一条官方 PR。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'news_ui.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    db.add_all(
        [
            Article(
                title=NORMAL_TITLE, category="动画", summary="制作決定とキャスト発表",
                body="body", source="机核 GCORES", original_url="https://n.example/1",
                is_published=True, published_at=datetime(2026, 10, 20, 12, 0),
                subcategory="", event_type="新作发布", region="日本",
                source_type="媒体编译", priority_score=129, is_adult=False,
            ),
            Article(
                title=ADULT_TITLE, category="游戏", summary="18禁美少女ゲーム発売",
                body="body", image_url="/static/img/adult.jpg", source="4Gamer",
                original_url="https://n.example/2", is_published=True,
                published_at=datetime(2026, 10, 20, 11, 0),
                subcategory="Galgame", event_type="发售", region="日本",
                source_type="媒体编译", priority_score=149, is_adult=True,
            ),
            Article(
                title="グッズ発売記念イベント開催", category="综合动态", summary="グッズ",
                body="body", source="ANIPLEX NEWS BOX", original_url="https://n.example/3",
                is_published=True, subcategory="", event_type="发售", region="日本",
                source_type="官方PR", priority_score=42, is_adult=False,
            ),
        ]
    )
    db.commit()
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_detail_lead_hidden_when_body_repeats_it():
    """详情页摘要与正文重复时不再单独渲染导语（导入时 summary 就是 body 前 200 字）。"""
    from types import SimpleNamespace

    from app.business_pages import article_detail_lead

    text = "游戏开发者今日在 Steam 公开了体验版，主打心理恐怖与复古像素画面。"
    dup = SimpleNamespace(summary=text, body=text + "\n后续补充说明。")
    assert article_detail_lead(dup) == ""

    distinct = SimpleNamespace(summary="一句话导语。", body="完全不同的正文开头，讲的是别的事情。")
    assert article_detail_lead(distinct) == "一句话导语。"

    empty = SimpleNamespace(summary="", body="正文")
    assert article_detail_lead(empty) == ""


def test_news_time_hides_fake_midnight():
    """源只给日期时（published_at 是 00:00）不显示 00:00，避免编造「凌晨发布」。"""
    from app.page_common import news_time

    assert news_time(datetime(2026, 10, 20, 0, 0)) == "2026-10-20"
    assert news_time(datetime(2026, 10, 20, 17, 35)) == "2026-10-20 17:35"
    assert news_time(None) == ""


def test_home_feed_folds_official_pr(news_client):
    """主页 feed 折叠官方 PR，正常与成人向资讯照常展示。"""
    resp = news_client.get("/")
    assert resp.status_code == 200
    assert NORMAL_TITLE in resp.text
    assert "グッズ発売記念イベント開催" not in resp.text  # 官方 PR 不进主 feed


def test_articles_page_lists_all_including_official_pr(news_client):
    """资讯列表页的契约是「全部资讯」：不折叠官方 PR，计数用真实总数而非当前页条数。

    首页精选（list_home_feed）会折叠官方 PR 并按类别配额裁剪，列表页不能沿用那套，
    否则官方 PR 与同类里靠后的资讯在全站就没有入口了。
    """
    resp = news_client.get("/articles")
    assert resp.status_code == 200
    assert NORMAL_TITLE in resp.text
    assert ADULT_TITLE in resp.text
    assert "グッズ発売記念イベント開催" in resp.text  # 官方 PR 在列表页可见
    assert "共 3 篇资讯" in resp.text


def test_articles_page_category_filter(news_client):
    """分类筛选按 ACGN 类型（不是来源）：只看某个分类时其他分类不出现。"""
    resp = news_client.get("/articles", params={"category": "综合动态"})
    assert resp.status_code == 200
    assert "グッズ発売記念イベント開催" in resp.text  # 官方 PR 的这条是综合动态
    assert NORMAL_TITLE not in resp.text  # 动画分类的不出现
    assert "共 1 篇资讯" in resp.text
    # 筛选条本身按分类渲染（来源不再是筛选入口）
    assert "category=" in resp.text
    assert "source=" not in resp.text


def test_articles_page_featured_only_for_anime(news_client):
    """大封推（头条位）只给动画类：默认视图与动画分类视图有头条，其他分类没有。"""
    default = news_client.get("/articles")
    assert "article-feature" in default.text  # 默认第一篇是动画，进头条位

    other = news_client.get("/articles", params={"category": "综合动态"})
    assert "article-feature" not in other.text  # 非动画分类不配大封推


def test_articles_page_paginates(news_client, monkeypatch):
    """分页生效：每页条数由 crud.ARTICLES_PAGE_SIZE 决定，翻页内容不同且回显页码。"""
    monkeypatch.setattr(crud, "ARTICLES_PAGE_SIZE", 2)
    page1 = news_client.get("/articles")
    assert page1.status_code == 200
    assert "第 1 / 2 页" in page1.text
    assert NORMAL_TITLE in page1.text  # 最新一篇在第一页头条位

    page2 = news_client.get("/articles", params={"page": 2})
    assert page2.status_code == 200
    assert "第 2 / 2 页" in page2.text
    assert NORMAL_TITLE not in page2.text

    # 页码越界夹回最后一页，不返回空白页
    over = news_client.get("/articles", params={"page": 99})
    assert over.status_code == 200
    assert "第 2 / 2 页" in over.text


def test_adult_content_hidden_by_default(news_client, monkeypatch):
    """默认开启隐藏：成人资讯的图片与露骨摘要被替换成普通提示，标题仍在。"""
    monkeypatch.setattr(settings, "news_hide_adult_content", True)
    resp = news_client.get("/articles")
    assert resp.status_code == 200
    assert ADULT_TITLE in resp.text  # 作品名 / 标题照常展示
    assert "/static/img/adult.jpg" not in resp.text  # 图片被隐藏
    assert "18禁美少女ゲーム発売" not in resp.text  # 露骨摘要被隐藏
    assert "已按站点设置隐藏" in resp.text
    # 正常资讯不受影响
    assert "制作決定とキャスト発表" in resp.text


def test_adult_summary_shown_when_setting_off(news_client, monkeypatch):
    """关闭隐藏设置后，成人资讯的摘要正常展示（开关是显示设置，不是内容删除）。"""
    monkeypatch.setattr(settings, "news_hide_adult_content", False)
    resp = news_client.get("/articles")
    assert resp.status_code == 200
    assert "18禁美少女ゲーム発売" in resp.text
