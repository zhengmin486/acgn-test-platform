"""数据同步（测试管理平台 → 数据管理 → 数据同步）功能测试。

覆盖：
- 权限（未登录跳登录 / 普通用户 403 / 管理员可见）；
- 入口与表单（总览入口、二级导航、三个数据源的表单与范围提示）；
- 预览 → 确认两步的页面行为（成功、范围不合法、任务占用、意外异常）；
- 三类数据源的真实流程（不触网：Bangumi 打桩 fetch、Yuc 打桩页面、资讯打桩假站点）；
- 重复数据与不覆盖本地数据、幂等（重复确认不产生重复数据）；
- 同步结果的展示与同步历史。

全部用例使用独立临时 SQLite（tmp_path）并覆盖 get_db 与
app.sync_tasks.SessionLocal，绝不触碰开发库 local_dev.db；
资讯抓取的网络层默认被打桩成「禁止访问网络」，需要数据的用例改用 tests/article_helpers
里的假站点 HTML，因此也不会访问真实网络。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import content_admin_pages, sync_tasks
from app.database import Base, get_db
from app.main import app
from app.models import AnimeSchedule, Article, Content, SyncTaskRun
from article_helpers import (
    SITE_BASE,
    detail_page,
    forbid_network,
    list_item,
    list_page,
    make_site,
)
from auth_helpers import create_accounts, login_as_admin, login_as_user
from scripts import import_articles
from scripts.import_bangumi import BangumiAPIError
from scripts.import_bangumi import run_import as real_run_import
from scripts.sync_yuc_schedule import run_sync as real_run_sync
from test_yuc_schedule import ATOM_URL, ATOM_XML, SEASON_HTML, SEASON_URL, make_stub_fetch

SYNC_PATH = "/manage/sync"

# 本地已存在的 Bangumi 条目：用于验证「重复导入不覆盖本地数据」。
EXISTING_BANGUMI_ID = 265
EXISTING_RATING = 1.5

# 本地已存在的资讯（对应假站点列表里的第 1 条）：用于验证「重复导入不覆盖本地数据」。
EXISTING_ARTICLE_ID = 71001
EXISTING_ARTICLE_TITLE = "本地已有资讯（不可被覆盖）"
EXISTING_ARTICLE_URL = f"{SITE_BASE}/detail.html?id={EXISTING_ARTICLE_ID}"


def _payload(bangumi_id: int, *, score: float = 7.5) -> dict:
    """构造一个可映射的 Bangumi 条目 payload（type=2 动画）。"""
    return {
        "id": bangumi_id,
        "type": 2,
        "name": f"entry-{bangumi_id}",
        "name_cn": f"条目{bangumi_id}",
        "summary": f"条目 {bangumi_id} 的简介。",
        "rating": {"score": score},
    }


def _stub_bangumi_fetch(payloads: dict[int, dict], *, fails: tuple[int, ...] = ()):
    """假 fetch(subject_id)：命中 payloads 返回数据，fails 中的 ID 报网络错误。"""

    def fetch(subject_id: int) -> dict:
        if subject_id in fails:
            raise BangumiAPIError(f"获取条目 {subject_id} 失败：网络错误")
        if subject_id not in payloads:
            raise BangumiAPIError(f"获取条目 {subject_id} 失败：HTTP 404 Not Found")
        return payloads[subject_id]

    return fetch


def _seed(session_factory) -> None:
    """播种三类数据：1 条已存在的 Bangumi 作品、1 篇已存在的资讯、2 条档期作品。"""
    db = session_factory()
    try:
        db.add(
            Content(
                title="本地已有条目（不可被覆盖）",
                type="anime",
                summary="本地简介",
                rating=EXISTING_RATING,
                bangumi_id=EXISTING_BANGUMI_ID,
                source_url=f"https://bgm.tv/subject/{EXISTING_BANGUMI_ID}",
            )
        )
        db.add(
            Article(
                title=EXISTING_ARTICLE_TITLE,
                category="动漫资讯",
                summary="本地摘要",
                body="本地正文",
                original_url=EXISTING_ARTICLE_URL,
                is_published=True,
            )
        )
        db.add_all([
            AnimeSchedule(season_code="202610", source_key="顶点武装", title_main="顶点武装",
                          weekday=6, platforms=[], source_url="http://yuc.wiki/202610/"),
            AnimeSchedule(season_code="202607", source_key="旧档期作品", title_main="旧档期作品",
                          weekday=1, platforms=[], source_url="http://yuc.wiki/202607/"),
        ])
        db.commit()
    finally:
        db.close()


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """独立临时库 + get_db / SessionLocal 双覆盖；游客 / 普通用户 / 管理员三个客户端。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sync_tasks.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    create_accounts(session_factory, admin=True, user=True)
    _seed(session_factory)

    # 页面与任务编排统一使用同一个临时库（既不碰开发库，也不跨库读历史）。
    monkeypatch.setattr(sync_tasks, "SessionLocal", session_factory)
    # 资讯抓取默认禁止真实网络：需要数据的用例再用 article_site 覆盖。
    monkeypatch.setattr(import_articles, "fetch_page", forbid_network)

    def override_get_db():
        db = session_factory()
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
                              session_factory=session_factory)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def article_site():
    """假 ANIPLEX NEWS BOX 站点：news 栏目 3 条资讯，第 1 条与本地已有记录同 URL。"""
    items = [
        list_item(EXISTING_ARTICLE_ID, "Oct 16, 2026", EXISTING_ARTICLE_TITLE),
        list_item(71002, "Oct 15, 2026", "资讯二"),
        list_item(71003, "Oct 14, 2026", "资讯三"),
    ]
    details = {
        EXISTING_ARTICLE_ID: detail_page(
            EXISTING_ARTICLE_TITLE, "Oct 16, 2026", "<p>源站正文（不应覆盖本地）</p>"
        ),
        71002: detail_page("资讯二", "Oct 15, 2026", "<p>第二条正文</p>"),
        71003: detail_page("资讯三", "Oct 14, 2026", "<p>第三条正文</p>"),
    }
    return make_site({f"{SITE_BASE}/?category=news": list_page(items)}, details)


def _count(data_env, model) -> int:
    db = data_env.session_factory()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def _titles(data_env, model, column="title"):
    """取出某列的全部值（按 id 升序），便于断言具体哪几条被写入。"""
    db = data_env.session_factory()
    try:
        return list(db.scalars(select(getattr(model, column)).order_by(model.id)))
    finally:
        db.close()


def _runs(data_env) -> list[SyncTaskRun]:
    db = data_env.session_factory()
    try:
        return list(db.scalars(select(SyncTaskRun).order_by(SyncTaskRun.id)))
    finally:
        db.close()


# ---------- 权限 ----------


def test_guest_is_redirected_to_login(sync_env):
    """未登录访问数据同步页：跳转登录页并带上返回地址。"""
    resp = sync_env.guest.get(SYNC_PATH, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/login?next={SYNC_PATH}"


def test_normal_user_is_forbidden(sync_env):
    """普通用户：页面 403，提交预览/确认同样 403（不只是隐藏入口）。"""
    assert sync_env.user.get(SYNC_PATH, follow_redirects=False).status_code == 403
    assert sync_env.user.post(
        "/manage/sync/preview", data={"task": "articles"}, follow_redirects=False
    ).status_code == 403
    assert sync_env.user.post(
        "/manage/sync/apply", data={"task": "articles"}, follow_redirects=False
    ).status_code == 403


def test_admin_can_open_sync_page(sync_env):
    """管理员可打开数据同步页：三个数据源与历史区块都在。"""
    resp = sync_env.admin.get(SYNC_PATH)
    assert resp.status_code == 200
    body = resp.text
    assert "数据同步" in body
    for label in ("Bangumi 条目", "新番档期", "官方资讯", "同步历史"):
        assert label in body


# ---------- 入口与表单 ----------


def test_data_overview_has_sync_entry(sync_env):
    """数据管理总览出现「进入数据同步」入口。"""
    body = sync_env.admin.get("/manage").text
    assert "/manage/sync" in body
    assert "进入数据同步" in body


def test_data_subnav_has_sync_entry(sync_env):
    """数据管理二级导航出现「数据同步」，且在同步页高亮。"""
    body = sync_env.admin.get("/manage/contents").text
    assert "数据同步" in body and "/manage/sync" in body
    assert 'href="/manage/sync"' in sync_env.admin.get(SYNC_PATH).text


def test_season_form_offers_local_seasons(sync_env):
    """新番档期表单：档期输入框可自由填写，建议列表含本地已同步档期。"""
    body = sync_env.admin.get(SYNC_PATH, params={"task": "yuc_schedule"}).text
    assert 'name="scope"' in body
    assert "202610" in body and "202607" in body
    assert "Yuc.wiki" in body


def test_season_form_allows_typing_new_season(sync_env):
    """档期不能只给「本地已同步」的下拉：那会让页面有数据后只剩老选项。"""
    body = sync_env.admin.get(SYNC_PATH, params={"task": "yuc_schedule"}).text
    # 自由输入框 + 建议列表，而不是只能选的 select
    assert '<input class="form-control form-control-sm" id="sync-scope" name="scope"' in body
    assert 'list="season-suggestions"' in body
    assert "<select" not in body
    assert "也可直接填任意 YYYYMM" in body


def test_suggest_season_codes_covers_local_and_nearby():
    """建议项 = 本地已同步档期（带条数）+ 今天前后三个月，按档期倒序。"""
    from datetime import datetime

    local = [
        {"season_code": "202610", "count": 12},
        {"season_code": "202604", "count": 3},
    ]
    items = sync_tasks.suggest_season_codes(local, today=datetime(2026, 10, 18))
    codes = [item["season_code"] for item in items]
    assert codes == sorted(codes, reverse=True)
    assert len(codes) == len(set(codes))
    # 前后三个月 + 本地已有的两个
    assert {"202607", "202610", "202611", "202701", "202604"} <= set(codes)
    labels = {item["season_code"]: item["label"] for item in items}
    assert labels["202610"] == "202610（本地 12 条）"
    assert labels["202701"] == "202701"  # 没同步过，不带本地条数
    assert next(i for i in items if i["season_code"] == "202604")["count"] == 3


def test_suggest_season_codes_handles_empty_local():
    """本地没有任何档期时也要有建议（否则用户不知道能填什么）。"""
    from datetime import datetime

    items = sync_tasks.suggest_season_codes([], today=datetime(2026, 1, 15))
    codes = [item["season_code"] for item in items]
    # 2026 年 1 月前后各三个月
    assert set(codes) == {
        "202510", "202511", "202512", "202601", "202602", "202603", "202604",
    }
    assert codes[0] == "202604"  # 倒序：最近的在前
    assert all(item["count"] is None for item in items)


def test_articles_form_offers_categories_and_limit(sync_env):
    """官方资讯表单：标明数据源、给出站点栏目下拉与条数上限输入。"""
    body = sync_env.admin.get(SYNC_PATH, params={"task": "articles"}).text
    assert "ANIPLEX NEWS BOX" in body and "news.aniplex.co.jp" in body
    assert "动漫资讯" in body and "活动资讯" in body
    assert 'name="limit"' in body
    assert f"本页最多 {sync_tasks.MAX_ARTICLE_LIMIT} 条" in body
    assert "不覆盖本地数据" in body


def test_bangumi_form_hints_max_ids(sync_env):
    """Bangumi 表单提示单次上限与去重规则。"""
    body = sync_env.admin.get(SYNC_PATH).text
    assert f"最多 {sync_tasks.MAX_BANGUMI_IDS} 个" in body
    assert "不覆盖本地数据" in body


# ---------- 预览 / 确认：页面行为（替换任务执行） ----------


def _preview_result(**overrides) -> sync_tasks.TaskResult:
    """构造一个预览结果对象（字段与真实执行一致）。"""
    data = dict(
        task_key=sync_tasks.TASK_BANGUMI,
        task_label="Bangumi 条目",
        source_label="Bangumi 官方 API（api.bgm.tv）",
        scope="265, 975",
        mode=sync_tasks.MODE_PREVIEW,
        pending=2,
        duplicates=1,
        failed=0,
        message="预览完成：可导入 2 条，未写入数据库",
        details=[
            {"status": "pending", "title": "条目A", "reason": "预览模式：未写入数据库"},
            {"status": "duplicates", "title": "条目B", "reason": "该 bangumi_id 已存在"},
        ],
    )
    data.update(overrides)
    return sync_tasks.TaskResult(**data)


def test_preview_shows_result_and_confirm_button(sync_env, monkeypatch):
    """预览成功：页面展示计数、明细与确认按钮，且以 apply=False 调用任务。"""
    calls = []

    def fake_run(task_key, scope, *, apply, scope_limit=None, actor_id=None):
        calls.append((task_key, scope, apply, scope_limit, actor_id))
        return _preview_result()

    monkeypatch.setattr(content_admin_pages, "run_sync_task", fake_run)
    resp = sync_env.admin.post(
        "/manage/sync/preview", data={"task": "bangumi", "scope": "265, 975"}
    )
    assert resp.status_code == 200
    body = resp.text
    assert "同步结果" in body and "预览" in body
    assert "确认写入" in body
    assert "条目A" in body and "条目B" in body
    assert 'name="scope" value="265, 975"' in body  # 确认表单回传原始范围

    # 确认入口必须在「逐条结果」明细表之前出现：明细可能很长，放在后面会被推到
    # 页面底部而让人以为只有预览、没有写入。与「预览」按钮并排一处 + 明细下方次要一处。
    first_confirm = body.find('action="/manage/sync/apply"')
    details_pos = body.find("逐条结果")
    assert first_confirm != -1 and details_pos != -1
    assert first_confirm < details_pos
    assert body.count('action="/manage/sync/apply"') == 2
    assert "sync-confirm-prominent" in body
    assert calls and calls[0][2] is False
    assert calls[0][4] is not None  # 记录了发起人（管理员主键）


@pytest.mark.parametrize(
    "exc,expected",
    [
        (sync_tasks.ScopeError("单次最多同步 30 个条目（当前 31 个）"), "最多同步"),
        (sync_tasks.TaskBusyError("「Bangumi 条目」已有同步任务在执行，请稍后再试"), "已有同步任务在执行"),
    ],
)
def test_preview_reports_scope_and_busy_errors(sync_env, monkeypatch, exc, expected):
    """范围不合法 / 任务占用：原页回显原因，不出现确认按钮，也不抛 500。"""

    def fake_run(*args, **kwargs):
        raise exc

    monkeypatch.setattr(content_admin_pages, "run_sync_task", fake_run)
    resp = sync_env.admin.post("/manage/sync/preview", data={"task": "bangumi", "scope": "x"})
    assert resp.status_code == 200
    assert expected in resp.text
    assert 'action="/manage/sync/apply"' not in resp.text


def test_preview_reports_unexpected_error(sync_env, monkeypatch):
    """预览中的意外异常：回显「预览失败」，页面不报错。"""

    def fake_run(*args, **kwargs):
        raise RuntimeError("内部错误")

    monkeypatch.setattr(content_admin_pages, "run_sync_task", fake_run)
    resp = sync_env.admin.post("/manage/sync/preview", data={"task": "bangumi", "scope": "1"})
    assert resp.status_code == 200
    assert "预览失败" in resp.text and "内部错误" in resp.text


def test_apply_redirects_to_run_detail(sync_env, monkeypatch):
    """确认写入：以 apply=True 调用任务，并重定向到该次运行的结果页。"""
    calls = []

    def fake_run(task_key, scope, *, apply, scope_limit=None, actor_id=None):
        calls.append((task_key, apply))
        return _preview_result(mode=sync_tasks.MODE_APPLY, added=2, pending=0, run_id=7)

    monkeypatch.setattr(content_admin_pages, "run_sync_task", fake_run)
    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "bangumi", "scope": "265, 975"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "run=7" in location and "task=bangumi" in location
    assert calls == [("bangumi", True)]


def test_apply_result_suggests_regression(sync_env):
    """正式写入的结果页给出下一步：跑一次回归批次，确认接口没被这次同步影响。"""
    db = sync_env.session_factory()
    try:
        run = SyncTaskRun(
            task_key=sync_tasks.TASK_BANGUMI, source_label="Bangumi",
            scope="1001", mode=sync_tasks.MODE_APPLY, status=sync_tasks.STATUS_OK,
            added=2, finished_at=datetime.now(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    body = sync_env.admin.get(f"/manage/sync?run={run_id}").text
    assert "建议执行一次回归批次" in body
    assert 'href="/testcases"' in body


def test_preview_result_has_no_regression_hint(sync_env):
    """预览没有写库，不提示跑回归——数据没变就不该打扰。"""
    db = sync_env.session_factory()
    try:
        run = SyncTaskRun(
            task_key=sync_tasks.TASK_BANGUMI, source_label="Bangumi",
            scope="1001", mode=sync_tasks.MODE_PREVIEW, status=sync_tasks.STATUS_OK,
            pending=2, finished_at=datetime.now(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    body = sync_env.admin.get(f"/manage/sync?run={run_id}").text
    assert "建议执行一次回归批次" not in body


def test_apply_reports_errors_via_redirect(sync_env, monkeypatch):
    """确认写入的失败：303 回显原因，不产生 500，也不写业务数据。"""

    def busy(*args, **kwargs):
        raise sync_tasks.TaskBusyError("「官方资讯」已有同步任务在执行，请稍后再试")

    monkeypatch.setattr(content_admin_pages, "run_sync_task", busy)
    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "articles"}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "error=" in location
    assert "已有同步任务在执行" in sync_env.admin.get(location).text

    def boom(*args, **kwargs):
        raise RuntimeError("网络中断")

    monkeypatch.setattr(content_admin_pages, "run_sync_task", boom)
    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "articles"}, follow_redirects=False
    )
    assert resp.status_code == 303
    page = sync_env.admin.get(resp.headers["location"]).text
    assert "同步未完成" in page and "网络中断" in page


# ---------- 真实流程：Bangumi ----------


@pytest.fixture
def stub_bangumi(monkeypatch):
    """把 sync_tasks 里的 run_import 换成注入假 fetch 的真实流程（不触网）。"""
    payloads = {
        1001: _payload(1001),
        1002: _payload(1002),
        1003: _payload(1003),
        # 本地已存在该条目（用于验证重复导入不覆盖本地数据）
        EXISTING_BANGUMI_ID: _payload(EXISTING_BANGUMI_ID, score=9.9),
    }

    def fake_run_import(ids, *, session_factory, apply=False, **kwargs):
        fetch = _stub_bangumi_fetch(payloads)
        return real_run_import(ids, session_factory=session_factory, apply=apply, fetch=fetch)

    monkeypatch.setattr(sync_tasks, "run_import", fake_run_import)
    return payloads


def test_bangumi_preview_does_not_write_then_apply_writes_once(sync_env, stub_bangumi):
    """Bangumi：预览不写库，确认后写入一次，重复确认不产生重复数据。"""
    before = _count(sync_env, Content)

    preview = sync_env.admin.post(
        "/manage/sync/preview", data={"task": "bangumi", "scope": "1001 1002"}
    )
    assert preview.status_code == 200
    assert "待写入" in preview.text
    assert _count(sync_env, Content) == before  # 预览不写业务数据

    apply_resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "bangumi", "scope": "1001 1002"},
        follow_redirects=False,
    )
    assert apply_resp.status_code == 303
    assert _count(sync_env, Content) == before + 2
    titles = _titles(sync_env, Content)
    assert "条目1001" in titles and "条目1002" in titles

    # 重复确认：全部命中唯一索引，不再新增。
    again = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "bangumi", "scope": "1001 1002"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert _count(sync_env, Content) == before + 2

    runs = _runs(sync_env)
    assert [r.mode for r in runs] == ["preview", "apply", "apply"]
    assert all(r.task_key == "bangumi" for r in runs)
    assert runs[1].added == 2 and runs[1].status == sync_tasks.STATUS_OK
    assert runs[2].added == 0 and runs[2].duplicates == 2
    assert runs[0].pending == 2 and runs[0].added == 0


def test_bangumi_duplicate_does_not_overwrite_local_data(sync_env, stub_bangumi):
    """已存在的 bangumi_id 只记为重复跳过，本地标题与评分保持不动。"""
    sync_env.admin.post(
        "/manage/sync/apply",
        data={"task": "bangumi", "scope": str(EXISTING_BANGUMI_ID)},
        follow_redirects=False,
    )
    db = sync_env.session_factory()
    try:
        row = db.scalar(select(Content).where(Content.bangumi_id == EXISTING_BANGUMI_ID))
        assert row.title == "本地已有条目（不可被覆盖）"
        assert row.rating == EXISTING_RATING
    finally:
        db.close()
    assert _runs(sync_env)[-1].duplicates == 1


def test_bangumi_partial_failure_is_reported(sync_env, monkeypatch):
    """部分条目拉取失败：成功写入的照常入库，失败计入统计并显示原因。"""

    def fake_run_import(ids, *, session_factory, apply=False, **kwargs):
        fetch = _stub_bangumi_fetch({1001: _payload(1001)}, fails=(1002,))
        return real_run_import(ids, session_factory=session_factory, apply=apply, fetch=fetch)

    monkeypatch.setattr(sync_tasks, "run_import", fake_run_import)
    before = _count(sync_env, Content)
    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "bangumi", "scope": "1001 1002"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _count(sync_env, Content) == before + 1
    run = _runs(sync_env)[-1]
    assert run.status == sync_tasks.STATUS_PARTIAL
    assert (run.added, run.failed) == (1, 1)
    page = sync_env.admin.get(resp.headers["location"]).text
    assert "部分成功" in page and "获取条目 1002 失败" in page


def test_bangumi_too_many_ids_is_rejected(sync_env, stub_bangumi):
    """超出单次上限：页面回显原因，不调用同步流程、不写记录。"""
    ids = " ".join(str(9000 + i) for i in range(sync_tasks.MAX_BANGUMI_IDS + 1))
    resp = sync_env.admin.post("/manage/sync/preview",
                               data={"task": "bangumi", "scope": ids})
    assert resp.status_code == 200
    assert f"最多同步 {sync_tasks.MAX_BANGUMI_IDS} 个" in resp.text
    assert _runs(sync_env) == []


@pytest.mark.parametrize("scope", ["", "abc", "0", "-3", "1 2 xx"])
def test_bangumi_invalid_scope_is_rejected(sync_env, stub_bangumi, scope):
    """非法条目 ID：回显错误，不发起同步。"""
    resp = sync_env.admin.post("/manage/sync/preview",
                               data={"task": "bangumi", "scope": scope})
    assert resp.status_code == 200
    assert "请填写至少一个" in resp.text or "必须是正整数" in resp.text
    assert _runs(sync_env) == []


def test_busy_task_is_rejected_while_running_record_exists(sync_env, stub_bangumi):
    """同一数据源已有未结束的运行记录：拒绝再次发起（避免并发重复同步）。"""
    db = sync_env.session_factory()
    try:
        db.add(SyncTaskRun(task_key=sync_tasks.TASK_BANGUMI, source_label="Bangumi",
                           scope="1001", mode=sync_tasks.MODE_APPLY,
                           status=sync_tasks.STATUS_RUNNING))
        db.commit()
    finally:
        db.close()

    resp = sync_env.admin.post("/manage/sync/apply",
                               data={"task": "bangumi", "scope": "1001"},
                               follow_redirects=False)
    assert resp.status_code == 303
    assert "已有同步任务在执行" in sync_env.admin.get(resp.headers["location"]).text
    assert [r.status for r in _runs(sync_env)] == [sync_tasks.STATUS_RUNNING]


# ---------- 真实流程：官方资讯（假站点，不触网） ----------


def test_articles_preview_then_apply_with_dedupe(sync_env, monkeypatch, article_site):
    """资讯：预览不写库；确认写入新条目；同 URL 已存在则跳过且不覆盖本地内容。"""
    monkeypatch.setattr(import_articles, "fetch_page", article_site)
    before = _count(sync_env, Article)

    preview = sync_env.admin.post(
        "/manage/sync/preview", data={"task": "articles"}
    )
    assert preview.status_code == 200
    assert "可新增 2 条" in preview.text and "已存在跳过 1 条" in preview.text
    assert _count(sync_env, Article) == before

    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "articles"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _count(sync_env, Article) == before + 2
    run = _runs(sync_env)[-1]
    assert (run.added, run.duplicates) == (2, 1)
    assert run.scope == f"分类=全部，上限={sync_tasks.ARTICLE_DEFAULT_LIMIT}"
    assert run.source_label and run.trigger == sync_tasks.TRIGGER_MANUAL

    # 本地已有条目（同 URL）保持原样：源站正文不覆盖本地摘要与正文。
    db = sync_env.session_factory()
    try:
        kept = db.scalar(select(Article).where(Article.original_url == EXISTING_ARTICLE_URL))
        assert (kept.summary, kept.body) == ("本地摘要", "本地正文")
    finally:
        db.close()

    # 再次确认：全部已存在，新增 0，标题不重复。
    sync_env.admin.post("/manage/sync/apply", data={"task": "articles"},
                        follow_redirects=False)
    assert _count(sync_env, Article) == before + 2
    assert _runs(sync_env)[-1].added == 0

    db = sync_env.session_factory()
    try:
        titles = list(db.scalars(select(Article.title)))
        assert len(titles) == len(set(titles))
    finally:
        db.close()


def test_articles_scope_category_and_limit(sync_env, monkeypatch, article_site):
    """资讯范围：按栏目 + 条数上限抓取（上限在抓取时生效，去重在抓取之后）。"""
    monkeypatch.setattr(import_articles, "fetch_page", article_site)
    resp = sync_env.admin.post(
        "/manage/sync/apply",
        data={"task": "articles", "scope": "动漫资讯", "limit": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    run = _runs(sync_env)[-1]
    assert run.scope == "分类=动漫资讯，上限=2"
    # 假站点前两条：第 1 条与本地已有记录同 URL -> 跳过，第 2 条写入。
    assert (run.added, run.duplicates) == (1, 1)
    titles = _titles(sync_env, Article)
    assert "资讯二" in titles and "资讯三" not in titles


def test_articles_detail_failure_is_partial(sync_env, monkeypatch):
    """某条详情页抓不到：该条不写库并计为失败，其余条目照常写入（部分成功）。"""
    items = [
        list_item(72001, "Oct 16, 2026", "抓不到正文的资讯"),
        list_item(72002, "Oct 15, 2026", "能抓到正文的资讯"),
    ]
    site = make_site(
        {f"{SITE_BASE}/?category=news": list_page(items)},
        {72002: detail_page("能抓到正文的资讯", "Oct 15, 2026", "<p>正文</p>")},
    )
    monkeypatch.setattr(import_articles, "fetch_page", site)

    resp = sync_env.admin.post("/manage/sync/apply", data={"task": "articles"},
                               follow_redirects=False)
    assert resp.status_code == 303
    run = _runs(sync_env)[-1]
    assert (run.added, run.failed) == (1, 1)
    assert run.status == sync_tasks.STATUS_PARTIAL
    assert "抓不到正文的资讯" not in _titles(sync_env, Article)


def test_articles_source_unavailable_reports_reason(sync_env, monkeypatch):
    """站点不可用：页面回显失败原因，运行记录标记失败，不写库也不抛 500。"""
    def failing_fetch(url):
        raise import_articles.ArticleFetchError(f"请求 {url} 失败：连接超时")

    monkeypatch.setattr(import_articles, "fetch_page", failing_fetch)
    before = _count(sync_env, Article)
    resp = sync_env.admin.post("/manage/sync/apply", data={"task": "articles"},
                               follow_redirects=False)
    assert resp.status_code == 303
    page = sync_env.admin.get(resp.headers["location"]).text
    assert "同步未完成" in page and "连接超时" in page
    assert _count(sync_env, Article) == before
    run = _runs(sync_env)[-1]
    assert run.status == sync_tasks.STATUS_FAILED and run.added == 0


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"task": "articles", "scope": "不存在分类"}, "未知的资讯分类"),
        ({"task": "articles", "limit": "0"}, "必须是正整数"),
        ({"task": "articles", "limit": str(sync_tasks.MAX_ARTICLE_LIMIT + 1)}, "最多同步"),
    ],
)
def test_articles_invalid_scope_is_rejected(sync_env, data, expected):
    """资讯范围非法：回显原因，不写库、不留记录。"""
    resp = sync_env.admin.post("/manage/sync/preview", data=data)
    assert resp.status_code == 200
    assert expected in resp.text
    assert _runs(sync_env) == []


# ---------- 真实流程：新番档期（打桩页面，不触网） ----------


def test_yuc_preview_then_apply_and_idempotent(sync_env, monkeypatch):
    """档期：预览不写新番数据；确认写入；重复同步只更新不变，不删本地记录。"""
    fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

    def fake_run_sync(season_code, *, session_factory, apply=False, **kwargs):
        return real_run_sync(season_code, session_factory=session_factory, apply=apply,
                             fetch=fetch)

    monkeypatch.setattr(sync_tasks, "run_sync", fake_run_sync)
    before = _count(sync_env, AnimeSchedule)

    preview = sync_env.admin.post(
        "/manage/sync/preview", data={"task": "yuc_schedule", "scope": "202610"}
    )
    assert preview.status_code == 200
    assert "预览" in preview.text and "未写入新番数据" in preview.text
    assert _count(sync_env, AnimeSchedule) == before

    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "yuc_schedule", "scope": "202610"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _count(sync_env, AnimeSchedule) > before
    run = _runs(sync_env)[-1]
    assert run.task_key == sync_tasks.TASK_YUC and run.added > 0

    # 再次同步：同一档期条目按 source_key 更新/不变，不新增重复，也不删除本地记录。
    counted = _count(sync_env, AnimeSchedule)
    sync_env.admin.post("/manage/sync/apply",
                        data={"task": "yuc_schedule", "scope": "202610"},
                        follow_redirects=False)
    assert _count(sync_env, AnimeSchedule) == counted
    assert _runs(sync_env)[-1].added == 0
    keys = _titles(sync_env, AnimeSchedule, "source_key")
    assert len(keys) == len(set(keys))


def test_yuc_invalid_season_and_missing_season(sync_env, monkeypatch):
    """档期格式非法：直接回显；档期不在 Atom 中：回显同步流程给出的原因。"""
    resp = sync_env.admin.post(
        "/manage/sync/preview", data={"task": "yuc_schedule", "scope": "2026"}
    )
    assert resp.status_code == 200
    assert "档期码格式应为 YYYYMM" in resp.text

    fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

    def fake_run_sync(season_code, *, session_factory, apply=False, **kwargs):
        return real_run_sync(season_code, session_factory=session_factory, apply=apply,
                             fetch=fetch)

    monkeypatch.setattr(sync_tasks, "run_sync", fake_run_sync)
    resp = sync_env.admin.post(
        "/manage/sync/apply", data={"task": "yuc_schedule", "scope": "199901"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = sync_env.admin.get(resp.headers["location"]).text
    assert "同步未完成" in page and "不存在" in page
    # 失败也留下记录（便于排查），但状态为失败且没有写入。
    run = _runs(sync_env)[-1]
    assert run.status == sync_tasks.STATUS_FAILED and run.added == 0


# ---------- 结果展示与历史 ----------


def test_run_history_and_detail_view(sync_env, monkeypatch, article_site):
    """同步历史列出每次运行，可按 ?run=<id> 回看该次结果与范围。"""
    monkeypatch.setattr(import_articles, "fetch_page", article_site)
    sync_env.admin.post("/manage/sync/apply", data={"task": "articles"},
                        follow_redirects=False)
    run = _runs(sync_env)[-1]

    body = sync_env.admin.get(SYNC_PATH).text
    assert "同步历史" in body
    assert f"run={run.id}" in body
    assert "正式写入" in body

    detail = sync_env.admin.get(SYNC_PATH, params={"run": run.id}).text
    assert "同步结果" in detail
    assert f"分类=全部，上限={sync_tasks.ARTICLE_DEFAULT_LIMIT}" in detail
    assert "本次为正式写入结果" in detail
    # 历史回看不提供确认按钮（确认表单只在刚预览完时出现）。
    assert 'action="/manage/sync/apply"' not in detail


def test_history_detail_shows_preview_marks(sync_env, monkeypatch, article_site):
    """历史里的预览记录：模式标注为预览、状态成功，并保留逐条明细。"""
    monkeypatch.setattr(import_articles, "fetch_page", article_site)
    sync_env.admin.post("/manage/sync/preview", data={"task": "articles"})
    run = _runs(sync_env)[-1]
    detail = sync_env.admin.get(SYNC_PATH, params={"run": run.id}).text
    assert "预览" in detail and "逐条结果" in detail
    assert 'action="/manage/sync/apply"' not in detail


def test_run_task_lock_is_released_between_runs(sync_env, monkeypatch, article_site):
    """连续两次任务都能执行（进程内互斥锁在结束后释放，不会互相阻塞）。"""
    monkeypatch.setattr(import_articles, "fetch_page", article_site)
    first = sync_tasks.run_task("articles", "", apply=False)
    second = sync_tasks.run_task("articles", "", apply=True)
    assert first.mode == sync_tasks.MODE_PREVIEW
    assert second.mode == sync_tasks.MODE_APPLY
    assert [r.mode for r in _runs(sync_env)] == ["preview", "apply"]


def test_unknown_task_key_is_rejected(sync_env):
    """未知数据源：抛 ScopeError（页面会回显原因，不会 500）。"""
    with pytest.raises(sync_tasks.ScopeError):
        sync_tasks.run_task("unknown_source", "x", apply=False)
