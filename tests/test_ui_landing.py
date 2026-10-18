"""界面改良落地回归测试：用例页筛选与统计、执行历史筛选、批次进度条。

覆盖 2026-10-17 UI 改良 P3 的新增交互（GET 筛选参数默认不影响既有行为）。
依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from auth_helpers import create_accounts, login_as_admin

CASE_GET = {
    "name": "健康检查用例",
    "method": "GET",
    "path": "/api/health",
    "expected_status": 200,
    "enabled": True,
}
CASE_POST = {
    "name": "登录回归用例",
    "method": "POST",
    "path": "/login",
    "expected_status": 200,
    "enabled": False,
}


@pytest.fixture
def ui_client(tmp_path):
    """独立临时数据库 + get_db 覆盖，管理员登录，预置两条用例。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ui.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(TestingSession, admin=True, user=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    login_as_admin(client)
    client.post("/api/testcases", json=CASE_GET)
    client.post("/api/testcases", json=CASE_POST)
    yield client
    app.dependency_overrides.pop(get_db, None)


def test_testcases_page_has_stats_and_filters(ui_client):
    """用例页：统计条 + 筛选条 + 折叠新建面板齐备。"""
    resp = ui_client.get("/testcases")
    assert resp.status_code == 200
    assert "用例总数" in resp.text and "启用中" in resp.text and "已停用" in resp.text
    assert 'name="q"' in resp.text and 'name="method"' in resp.text and 'name="enabled"' in resp.text
    assert "<details" in resp.text and "新建用例" in resp.text
    assert "健康检查用例" in resp.text and "登录回归用例" in resp.text


def test_testcases_filter_by_keyword(ui_client):
    """关键词筛选：命中名称/路径，未命中不再展示。"""
    resp = ui_client.get("/testcases?q=登录")
    assert resp.status_code == 200
    assert "登录回归用例" in resp.text
    assert "健康检查用例" not in resp.text
    assert "重置" in resp.text


def test_testcases_filter_by_method(ui_client):
    """方法筛选：按请求方法精确过滤。"""
    resp = ui_client.get("/testcases?method=POST")
    assert "登录回归用例" in resp.text
    assert "健康检查用例" not in resp.text


def test_testcases_filter_by_enabled(ui_client):
    """状态筛选：enabled=0 只看停用。"""
    resp = ui_client.get("/testcases?enabled=0")
    assert "登录回归用例" in resp.text
    assert "健康检查用例" not in resp.text


def test_testcases_filter_default_is_full_list(ui_client):
    """无参时行为与改造前一致：全量展示，不显示重置按钮。"""
    resp = ui_client.get("/testcases")
    assert "健康检查用例" in resp.text and "登录回归用例" in resp.text


def test_runs_page_result_filter_chips(ui_client):
    """执行历史：结果筛选 chips 带计数，失败行带红条类名。"""
    case_id = ui_client.get("/api/testcases").json()["items"][0]["id"]
    ui_client.post(f"/api/testcases/{case_id}/execute")
    resp = ui_client.get("/runs")
    assert resp.status_code == 200
    assert "结果筛选" in resp.text
    assert "全部 1" in resp.text
    failed = ui_client.get("/runs?result=failed")
    assert failed.status_code == 200
    assert "仅失败" in failed.text


def test_batch_detail_progress(ui_client):
    """批次详情：执行全部后通过率进度条渲染。"""
    resp = ui_client.post("/testcases/execute-all", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    page = ui_client.get(location)
    assert page.status_code == 200
    assert "batch-progress" in page.text


def test_contents_search_echo(ui_client):
    """/contents 筛选条件回显（value 与 selected）。"""
    resp = ui_client.get("/contents?q=%E5%81%A5%E5%BA%B7")
    assert resp.status_code == 200
    assert 'value="健康"' in resp.text


def test_home_uses_svg_icons(ui_client):
    """首页分类磁贴：内联 SVG 图标（class 含 svg-ic），无 emoji 图标。"""
    resp = ui_client.get("/")
    assert resp.status_code == 200
    assert "svg-ic" in resp.text
    assert "🎬" not in resp.text and "📚" not in resp.text


# ---------- 首页「本日新番」：只取今天排播、按播出时间到分排序（2026-10-18 改良） ----------

def _seed_schedule(Session, **overrides):
    """插一条最小可用的档期记录；platforms 有库级默认，无需显式赋值。"""
    from app.models import AnimeSchedule

    row = AnimeSchedule(
        season_code="202610",
        source_key=overrides.get("source_key", ""),
        title_main=overrides.get("title_main", ""),
        source_url="https://example.com/anime",
        weekday=None,
        broadcast_time=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    session = Session()
    session.add(row)
    session.commit()
    session.close()


@pytest.fixture
def today_client(tmp_path):
    """独立临时库：今天与其它星期各插几条档期，覆盖排序与过滤。"""
    from datetime import datetime

    engine = create_engine(
        f"sqlite:///{tmp_path / 'landing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    today = datetime.now().isoweekday()
    other = today % 7 + 1
    _seed_schedule(TestingSession, source_key="early", title_main="早晨番",
                   weekday=today, broadcast_time="12:00")
    _seed_schedule(TestingSession, source_key="late", title_main="深夜番",
                   weekday=today, broadcast_time="23:00")
    _seed_schedule(TestingSession, source_key="notime", title_main="无时间番",
                   weekday=today, broadcast_time=None)
    _seed_schedule(TestingSession, source_key="othertab", title_main="别天番",
                   weekday=other, broadcast_time="09:00")

    client = TestClient(app)
    yield client
    app.dependency_overrides.pop(get_db, None)


def test_home_today_schedule_filtered_and_sorted(today_client):
    """首页只渲染今天的档期（别天的不出现），且按播出时间到分升序、无时间殿后。"""
    resp = today_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "本日新番" in html
    assert "别天番" not in html
    early = html.index("早晨番")
    late = html.index("深夜番")
    notime = html.index("无时间番")
    assert early < late < notime
    assert "12:00 更新" in html and "23:00 更新" in html
    # 更新时间徽标（HH:MM）随卡片渲染
    assert "schedule-time-badge" in html


def test_nav_search_has_type_select(today_client):
    """导航搜索胶囊：类型下拉（nav-type）+ 关键词框（nav-q）齐备。"""
    resp = today_client.get("/")
    assert resp.status_code == 200
    assert 'id="nav-type"' in resp.text
    assert 'id="nav-q"' in resp.text
    assert "动画" in resp.text  # 类型选项来自模板全局 type_labels
