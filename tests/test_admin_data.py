"""数据管理（测试管理平台 → 数据管理）功能测试。

覆盖：管理权限（未登录跳登录 / 普通用户 403）、三类数据的查看、
单条与批量删除、重新同步的成功与失败回显，以及删除不影响其它模块数据。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离；
重新同步通过替换 app.content_admin_pages.resync_season 桩函数完成，不触网、不写真实库。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import content_admin_pages
from app.database import Base, get_db
from app.main import app
from app.models import AnimeSchedule, Article, Content, ExecutionBatch
from auth_helpers import create_accounts, login_as_admin, login_as_user
from scripts.sync_yuc_schedule import YucFetchError

CONTENTS = [
    ("作品A", "anime", "简介A", 8.5),
    ("作品B", "comic", "简介B", 7.0),
    ("作品C", "game", "简介C", 9.0),
]


def _seed(session_factory) -> None:
    """播种三类数据：3 条作品、2 篇资讯（1 篇未发布）、2 个档期的 3 条档期作品。"""
    db = session_factory()
    try:
        db.add_all(
            Content(title=title, type=ctype, summary=summary, rating=rating)
            for title, ctype, summary, rating in CONTENTS
        )
        db.add_all([
            Article(title="资讯一", category="动画", summary="摘要一", body="正文一",
                    is_published=True),
            Article(title="资讯二", category="综合资讯", summary="摘要二", body="正文二",
                    is_published=False),
        ])
        db.add_all([
            AnimeSchedule(season_code="202610", source_key="作品x", title_main="作品X",
                          weekday=1, platforms=[], source_url="https://yuc.wiki/202610/"),
            AnimeSchedule(season_code="202610", source_key="作品y", title_main="作品Y",
                          weekday=None,
                          platforms=[{"name": "bilibili", "url": "https://www.bilibili.com/"}],
                          source_url="https://yuc.wiki/202610/"),
            AnimeSchedule(season_code="202607", source_key="作品z", title_main="作品Z",
                          weekday=3, platforms=[], source_url="https://yuc.wiki/202607/"),
        ])
        db.commit()
    finally:
        db.close()


@pytest.fixture
def data_env(tmp_path):
    """独立临时库 + get_db 覆盖；同一库上提供游客 / 普通用户 / 管理员三个客户端。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admin_data.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(testing_session, admin=True, user=True)
    _seed(testing_session)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    guest = TestClient(app)
    user = TestClient(app)
    login_as_user(user)
    admin = TestClient(app)
    login_as_admin(admin)
    try:
        yield SimpleNamespace(guest=guest, user=user, admin=admin,
                              session_factory=testing_session)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _count(data_env, model) -> int:
    db = data_env.session_factory()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def _ids(data_env, model, **filters):
    """按条件取出主键列表（按 id 升序），便于断言具体删除了哪几条。"""
    db = data_env.session_factory()
    try:
        stmt = select(model.id).order_by(model.id)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        return list(db.scalars(stmt))
    finally:
        db.close()


# ---------- 权限：三种身份 ----------

ADMIN_PAGES = ["/manage", "/manage/contents",
               "/manage/articles", "/manage/schedules"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_guest_is_redirected_to_login(data_env, path):
    """未登录访问数据管理任何页面：跳转登录页并带上返回地址。"""
    resp = data_env.guest.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/login?next={path}"


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_normal_user_is_forbidden(data_env, path):
    """普通用户访问数据管理页面：渲染 403，而不是仅隐藏入口。"""
    resp = data_env.user.get(path, follow_redirects=False)
    assert resp.status_code == 403
    assert "权限" in resp.text or "403" in resp.text


def test_normal_user_cannot_delete(data_env):
    """普通用户直接提交删除请求同样被拒绝，且数据不变。"""
    content_id = _ids(data_env, Content)[0]
    resp = data_env.user.post(
        "/manage/contents/delete", data={"ids": str(content_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert _count(data_env, Content) == len(CONTENTS)

    article_id = _ids(data_env, Article)[0]
    resp = data_env.user.post(f"/manage/articles/{article_id}/delete",
                              follow_redirects=False)
    assert resp.status_code == 403

    schedule_id = _ids(data_env, AnimeSchedule)[0]
    resp = data_env.user.post(f"/manage/schedules/{schedule_id}/delete",
                              follow_redirects=False)
    assert resp.status_code == 403
    assert _count(data_env, Article) == 2
    assert _count(data_env, AnimeSchedule) == 3


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_can_open_admin_pages(data_env, path):
    """管理员可以正常打开数据管理的全部页面。"""
    resp = data_env.admin.get(path)
    assert resp.status_code == 200


# ---------- 总览 ----------

def test_overview_shows_counts_and_entries(data_env):
    """总览展示三类数据规模，并提供各自入口。"""
    body = data_env.admin.get("/manage").text
    assert "作品数据" in body and "资讯数据" in body and "新番档期" in body
    for path in ("/manage/contents", "/manage/articles", "/manage/schedules"):
        assert path in body
    # 3 条作品 / 2 篇资讯 / 3 条档期（2 个档期）
    assert ">3<" in body
    assert ">2<" in body


# ---------- 作品数据 ----------

def test_contents_list_renders_rows_and_links(data_env):
    """作品列表展示全部条目，并提供查看与删除入口，未勾选也能单条删除。"""
    body = data_env.admin.get("/manage/contents").text
    for title, *_ in CONTENTS:
        assert title in body
    content_id = _ids(data_env, Content)[0]
    assert f'href="/contents/{content_id}"' in body
    assert "批量删除选中" in body
    assert "data-subnav" in body


def test_contents_keyword_filter(data_env):
    """关键词筛选只保留命中条目。"""
    body = data_env.admin.get("/manage/contents", params={"q": "作品A"}).text
    assert "作品A" in body
    assert "作品B" not in body


def test_delete_single_content(data_env):
    """单条删除：重定向回列表并回显删除条数，其它条目保留。"""
    deleted_id, kept_id = _ids(data_env, Content)[:2]
    resp = data_env.admin.post("/manage/contents/delete",
                               data={"ids": str(deleted_id)},
                               follow_redirects=False)
    assert resp.status_code == 303
    assert f"deleted=1" in resp.headers["location"]
    remaining = _ids(data_env, Content)
    assert deleted_id not in remaining and kept_id in remaining
    assert _count(data_env, Content) == len(CONTENTS) - 1


def test_delete_contents_batch(data_env):
    """批量删除：一次提交多个 id（重复的 ids 字段），全部删除。"""
    first, second, third = _ids(data_env, Content)
    resp = data_env.admin.post(
        "/manage/contents/delete",
        data={"ids": [str(first), str(second)]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "deleted=2" in resp.headers["location"]
    assert _ids(data_env, Content) == [third]


def test_delete_contents_without_selection_is_noop(data_env):
    """未勾选任何条目：不删除、不报错，回显 0 条。"""
    resp = data_env.admin.post("/manage/contents/delete", data={},
                               follow_redirects=False)
    assert resp.status_code == 303
    assert "deleted=0" in resp.headers["location"]
    assert _count(data_env, Content) == len(CONTENTS)


def test_delete_contents_keeps_other_tables(data_env):
    """删除作品不影响资讯与档期数据（单表删除，无级联清理）。"""
    content_id = _ids(data_env, Content)[0]
    data_env.admin.post("/manage/contents/delete", data={"ids": str(content_id)},
                        follow_redirects=False)
    assert _count(data_env, Article) == 2
    assert _count(data_env, AnimeSchedule) == 3


# ---------- 资讯数据 ----------

def test_articles_list_includes_unpublished(data_env):
    """管理视角包含未发布文章，并标注发布状态。"""
    body = data_env.admin.get("/manage/articles").text
    assert "资讯一" in body and "资讯二" in body
    assert "已发布" in body and "未发布" in body


def test_delete_article(data_env):
    """删除单篇资讯：重定向并回显，另一篇保留。"""
    article_id = _ids(data_env, Article)[0]
    resp = data_env.admin.post(f"/manage/articles/{article_id}/delete",
                               follow_redirects=False)
    assert resp.status_code == 303
    assert "deleted=1" in resp.headers["location"]
    assert article_id not in _ids(data_env, Article)
    assert _count(data_env, Article) == 1


def test_delete_missing_article_returns_404(data_env):
    """删除不存在的资讯：返回 404 页面并落在管理平台布局。"""
    resp = data_env.admin.post("/manage/articles/999999/delete",
                               follow_redirects=False)
    assert resp.status_code == 404
    assert "返回资讯数据" in resp.text


# ---------- 新番档期 ----------

def test_schedules_list_and_actions(data_env):
    """档期列表展示条目、查看入口与重新同步/删除操作。"""
    body = data_env.admin.get("/manage/schedules").text
    assert "作品X" in body and "作品Y" in body and "作品Z" in body
    assert "/schedule?season=202610" in body
    assert "重新同步" in body
    assert ">202607<" in body.replace("\n", "")


def test_schedules_season_filter(data_env):
    """按档期筛选：只保留该档期作品。"""
    body = data_env.admin.get("/manage/schedules", params={"season": "202607"}).text
    assert "作品Z" in body
    assert "作品X" not in body


def test_delete_schedule(data_env):
    """删除单条档期作品：重定向并回显，其它档期数据保留。"""
    schedule_id = _ids(data_env, AnimeSchedule)[0]
    resp = data_env.admin.post(f"/manage/schedules/{schedule_id}/delete",
                               follow_redirects=False)
    assert resp.status_code == 303
    assert "deleted=1" in resp.headers["location"]
    assert schedule_id not in _ids(data_env, AnimeSchedule)
    assert _count(data_env, AnimeSchedule) == 2


def test_delete_missing_schedule_returns_404(data_env):
    """删除不存在的档期作品：404 页面 + 管理平台布局。"""
    resp = data_env.admin.post("/manage/schedules/999999/delete",
                               follow_redirects=False)
    assert resp.status_code == 404
    assert "返回新番档期" in resp.text


# ---------- 重新同步 ----------

class _StubSummary:
    """替代真实同步汇总：只提供页面回显需要的计数。"""

    added = 2
    updated = 1
    unchanged = 5
    failed = 0


def test_resync_success_echoes_counts(data_env, monkeypatch):
    """重新同步成功：回显新增/更新/不变/失败计数，未删除任何本地数据。"""
    monkeypatch.setattr(content_admin_pages, "resync_season", lambda code: _StubSummary())
    resp = data_env.admin.post("/manage/schedules/resync",
                               data={"season_code": "202610"}, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "resync=ok" in location
    assert "added=2" in location and "updated=1" in location and "unchanged=5" in location

    page = data_env.admin.get(location).text
    assert "重新同步完成" in page
    assert _count(data_env, AnimeSchedule) == 3


def test_resync_network_failure_shows_reason(data_env, monkeypatch):
    """重新同步遇到网络错误：回显原因，页面不报错、数据不变。"""

    def _boom(code):
        raise YucFetchError("请求 https://yuc.wiki 失败：网络错误")

    monkeypatch.setattr(content_admin_pages, "resync_season", _boom)
    resp = data_env.admin.post("/manage/schedules/resync",
                               data={"season_code": "202610"}, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "resync=error" in location

    page = data_env.admin.get(location).text
    assert "重新同步未完成" in page
    assert _count(data_env, AnimeSchedule) == 3


def test_resync_without_season_reports_error(data_env, monkeypatch):
    """缺少档期参数：直接回显错误，不调用同步函数。"""

    def _unexpected(code):  # pragma: no cover - 该分支不应被执行
        raise AssertionError("缺少档期参数时不应触发同步")

    monkeypatch.setattr(content_admin_pages, "resync_season", _unexpected)
    resp = data_env.admin.post("/manage/schedules/resync", data={},
                               follow_redirects=False)
    assert resp.status_code == 303
    assert "resync=error" in resp.headers["location"]


def test_resync_unexpected_error_is_reported(data_env, monkeypatch):
    """同步过程中的意外异常也被捕获并回显，不导致 500。"""

    def _boom(code):
        raise RuntimeError("内部错误")

    monkeypatch.setattr(content_admin_pages, "resync_season", _boom)
    resp = data_env.admin.post("/manage/schedules/resync",
                               data={"season_code": "202610"}, follow_redirects=False)
    assert resp.status_code == 303
    page = data_env.admin.get(resp.headers["location"]).text
    assert "重新同步未完成" in page


# ---------- 导航与布局 ----------

def test_manage_pages_render_in_content_platform(data_env):
    """内容管理页在内容平台布局内，二级导航保留作品数据入口（不再套测试平台的壳）。"""
    body = data_env.admin.get("/manage").text
    assert "ACGN 内容平台" in body
    assert "/manage/contents" in body
    assert "测试管理平台" not in body


def test_manage_pages_share_content_subnav(data_env):
    """每个内容管理页都带内容管理二级导航（内容 / 站点两组），且不在测试平台壳内。"""
    for path in ("/manage/contents", "/manage/articles",
                 "/manage/schedules", "/manage/sync"):
        body = data_env.admin.get(path).text
        assert "ACGN 内容平台" in body
        assert "测试管理平台" not in body
        assert "/manage/submissions" in body
        assert "站点设置" in body


def test_test_platform_nav_has_no_content_management(data_env):
    """测试平台导航不再承载内容管理入口，只保留返回内容平台这一个跨域入口。"""
    body = data_env.admin.get("/testcases").text
    assert "/manage" not in body
    assert "数据管理" not in body
    assert "返回内容平台" in body


def test_content_platform_has_content_management_entry_for_admin(data_env):
    """内容平台导航出现「内容管理」入口（仅管理员 / 站长），指向现有管理页。"""
    body = data_env.admin.get("/").text
    assert "内容管理" in body
    assert "/manage" in body


def test_content_platform_hides_content_management_for_user(data_env):
    """普通用户的内容平台导航不出现「内容管理」入口。"""
    body = data_env.user.get("/").text
    assert "/manage" not in body


# ---------- 回归检查（总览页质量条） ----------


def _add_batch(data_env, *, total: int = 4, passed: int = 3, hours_ago: int = 0) -> int:
    """直接插一条批次快照（不跑真实用例），用于质量条展示。"""
    db = data_env.session_factory()
    try:
        finished = datetime.now() - timedelta(hours=hours_ago)
        batch = ExecutionBatch(total=total, passed=passed, failed=total - passed,
                               started_at=finished, finished_at=finished)
        db.add(batch)
        db.commit()
        db.refresh(batch)
        return batch.id
    finally:
        db.close()


def test_overview_shows_regression_entry_without_batches(data_env):
    """没有回归批次：总览显示空态，并给出执行入口。"""
    body = data_env.admin.get("/manage").text
    assert "回归检查" in body
    assert "尚未执行回归批次" in body
    assert 'href="/testcases"' in body


def test_overview_shows_latest_batch(data_env):
    """有批次：显示批次号、通过数、通过率与查看入口（口径 passed/total）。"""
    batch_id = _add_batch(data_env, total=4, passed=3)
    body = data_env.admin.get("/manage").text
    assert f"#{batch_id}" in body
    assert "3/4" in body
    assert "75.0%" in body
    assert f'href="/batches/{batch_id}"' in body


def test_overview_marks_stale_batch(data_env):
    """超过 24 小时的批次标为「可能已过期」——旧结果不代表当前代码。"""
    _add_batch(data_env, hours_ago=30)
    body = data_env.admin.get("/manage").text
    assert "可能已过期" in body
    assert "1 天前" in body


def test_overview_handles_empty_batch(data_env):
    """空批次（含 0 用例）显示为空批次，而不是 0% 通过率。"""
    _add_batch(data_env, total=0, passed=0)
    body = data_env.admin.get("/manage").text
    assert "空批次（没有启用用例）" in body
    assert "（0%）" not in body
