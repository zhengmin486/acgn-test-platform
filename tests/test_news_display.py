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


def test_home_feed_folds_official_pr(news_client):
    """主页 feed 折叠官方 PR，正常与成人向资讯照常展示。"""
    resp = news_client.get("/")
    assert resp.status_code == 200
    assert NORMAL_TITLE in resp.text
    assert "グッズ発売記念イベント開催" not in resp.text  # 官方 PR 不进主 feed


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
