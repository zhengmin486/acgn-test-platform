"""作品申请：登录用户提交新增 / 修改 → 管理员审核 → 通过后写入内容库。

覆盖：
- 访问控制：游客跳登录、用户可开表单、后台审核页仅管理员；
- 提交新增：进入 pending，审核前不写入 contents；
- 字段校验：空标题 / 非法类型都回 400 且不落库；
- 评分不在申请范围内：表单无评分输入，多余字段被忽略，通过审核也不改作品评分；
- 提交修改：记录 content_id，审核前不改动原作品；
- 审核通过：新增写库、修改生效，并记录 applied_content_id；
- 审核驳回：写驳回说明，内容库不变；
- 幂等：同一申请重复审核，第二次返回错误且不重复写库。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from auth_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    USER_USERNAME,
    create_accounts,
    login_as_admin,
    login_as_user,
)


@pytest.fixture
def sub_env(tmp_path):
    """独立临时库 + 种子作品 + 普通用户 / 管理员账号。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'submissions.db'}",
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
    try:
        yield SimpleNamespace(
            client=TestClient(app, follow_redirects=False),
            session_factory=TestingSession,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _uid(session_factory, username: str) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, username).id
    finally:
        db.close()


def _pending(session_factory):
    """取当前所有待审核申请（读库后返回已加载的 ORM 对象）。"""
    db = session_factory()
    try:
        _, rows = crud.list_content_submissions(
            db, status=crud.SUBMISSION_STATUS_PENDING
        )
        return rows
    finally:
        db.close()


def _submit_create(client, **overrides):
    data = {
        "title": "新词条测试",
        "type": "anime",
        "summary": "用于作品申请测试的简介。",
    }
    data.update(overrides)
    return client.post("/submit", data=data, follow_redirects=False)


class TestSubmissionAccess:
    def test_guest_redirected_to_login(self, sub_env):
        resp = sub_env.client.get("/submit")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/submit"

    def test_user_can_open_form(self, sub_env):
        login_as_user(sub_env.client)
        resp = sub_env.client.get("/submit")
        assert resp.status_code == 200
        assert "申请新增作品" in resp.text
        assert 'name="type"' in resp.text

    def test_edit_missing_content_returns_404(self, sub_env):
        login_as_user(sub_env.client)
        assert sub_env.client.get("/contents/9999/edit").status_code == 404


class TestSubmit:
    def test_create_submission_is_pending_and_not_applied(self, sub_env):
        login_as_user(sub_env.client)
        resp = _submit_create(sub_env.client)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=submissions&submitted=create"

        rows = _pending(sub_env.session_factory)
        assert len(rows) == 1
        assert rows[0].kind == crud.SUBMISSION_KIND_CREATE

        # 审核通过前不写入 contents
        db = sub_env.session_factory()
        try:
            total, _ = crud.search_contents_page(db, keyword="新词条测试")
            assert total == 0
        finally:
            db.close()

        page = sub_env.client.get("/me?tab=submissions")
        assert "新词条测试" in page.text
        assert "待审核" in page.text

    def test_validation_errors_render_form(self, sub_env):
        login_as_user(sub_env.client)
        empty = _submit_create(sub_env.client, title="  ")
        assert empty.status_code == 400
        assert "标题不能为空" in empty.text

        bad_type = _submit_create(sub_env.client, type="movie")
        assert bad_type.status_code == 400
        assert "类型不合法" in bad_type.text

        assert _pending(sub_env.session_factory) == []

    def test_update_submission_does_not_touch_content(self, sub_env):
        login_as_user(sub_env.client)
        resp = sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（改）",
                "type": "anime",
                "summary": "修改后的简介",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=submissions&submitted=update"

        rows = _pending(sub_env.session_factory)
        assert len(rows) == 1
        assert rows[0].kind == crud.SUBMISSION_KIND_UPDATE
        assert rows[0].content_id == 1

        db = sub_env.session_factory()
        try:
            assert crud.get_content(db, 1).title == "星海航线"
        finally:
            db.close()

    def test_edit_form_prefilled(self, sub_env):
        login_as_user(sub_env.client)
        page = sub_env.client.get("/contents/1/edit")
        assert page.status_code == 200
        assert "星海航线" in page.text
        # 评分不再预填（表单没有这个输入）；标题与简介照旧回填
        assert 'value="星海航线"' in page.text
        assert 'name="rating"' not in page.text
        # 修改态顶部展示当前评分，并说明它来自外部数据源
        assert "当前评分 8.7" in page.text
        assert "本流程不能修改" in page.text


class TestAdminReview:
    def test_admin_page_requires_admin(self, sub_env):
        assert sub_env.client.get("/manage/submissions").status_code == 303  # 游客
        login_as_user(sub_env.client)
        assert sub_env.client.get("/manage/submissions").status_code == 403
        login_as_admin(sub_env.client)
        assert sub_env.client.get("/manage/submissions").status_code == 200

    def test_approve_create_writes_content(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        page = sub_env.client.get("/manage/submissions?status=pending")
        assert "新词条测试" in page.text

        resp = sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "decided=ok" in resp.headers["location"]

        db = sub_env.session_factory()
        try:
            approved = crud.get_content_submission(db, row.id)
            assert approved.status == crud.SUBMISSION_STATUS_APPROVED
            assert approved.applied_content_id is not None
            created = crud.get_content(db, approved.applied_content_id)
            assert created.title == "新词条测试"
        finally:
            db.close()

        listing = sub_env.client.get("/contents?q=新词条测试")
        assert "新词条测试" in listing.text

    def test_approve_update_applies_fields(self, sub_env):
        login_as_user(sub_env.client)
        sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（新）",
                "type": "anime",
                "summary": "新简介",
            },
        )
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        resp = sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        assert resp.status_code == 303

        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            assert content.title == "星海航线（新）"
            assert content.summary == "新简介"
            # 评分不在申请范围内：修改通过也不该动作品评分（种子值 8.7）
            assert content.rating == 8.7
        finally:
            db.close()

    def test_reject_keeps_content_and_records_note(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        resp = sub_env.client.post(
            f"/manage/submissions/{row.id}/reject",
            data={"note": "信息不完整"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "status=rejected" in resp.headers["location"]

        db = sub_env.session_factory()
        try:
            rejected = crud.get_content_submission(db, row.id)
            assert rejected.status == crud.SUBMISSION_STATUS_REJECTED
            assert rejected.review_note == "信息不完整"
            assert crud.search_contents_page(db, keyword="新词条测试")[0] == 0
        finally:
            db.close()

    def test_double_review_is_rejected(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        first = sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        assert "decided=ok" in first.headers["location"]
        second = sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        assert "decided=error" in second.headers["location"]

        db = sub_env.session_factory()
        try:
            # 只写了一条作品，没有因为重复审核产生第二条
            assert crud.search_contents_page(db, keyword="新词条测试")[0] == 1
        finally:
            db.close()

    def test_user_cannot_approve(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        resp = sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        assert resp.status_code == 403


class TestObjectiveFields:
    """申请能覆盖详情页展示的客观信息，而不是只有标题 / 简介 / 评分。

    作品库现在的详情页会展示年份 / 媒介 / 话数 / 原作载体，申请表单若只有
    四项基本字段，通过审核后落地的就是一条残缺条目，与页面结构对不上。
    """

    def test_create_carries_objective_fields_into_content(self, sub_env):
        """新增申请带上客观信息，通过后要一并写进 contents。"""
        login_as_user(sub_env.client)
        resp = _submit_create(
            sub_env.client,
            title="带客观信息的新作",
            year="2024",
            media="TV",
            episodes="12",
            source_material="漫画原作",
        )
        assert resp.status_code == 303
        row = _pending(sub_env.session_factory)[0]
        assert (row.year, row.media, row.episodes, row.source_material) == (
            2024,
            "TV",
            12,
            "漫画原作",
        )

        login_as_admin(sub_env.client)
        sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        db = sub_env.session_factory()
        try:
            content = crud.find_content_by_title(db, "带客观信息的新作")
            assert content is not None
            assert content.year == 2024
            assert content.media == "TV"
            assert content.episodes == 12
            assert content.source_material == "漫画原作"
        finally:
            db.close()

    def test_update_keeps_objective_fields_when_left_blank(self, sub_env):
        """修改申请里留空 = 不改动原值：只改简介不该顺手把年份清空。"""
        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            content.year = 2021
            content.media = "剧场版"
            content.episodes = 1
            content.source_material = "原创"
            db.commit()
        finally:
            db.close()

        login_as_user(sub_env.client)
        resp = sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（新）",
                "type": "anime",
                "summary": "新简介",
                "year": "",
                "media": "",
                "episodes": "",
                "source_material": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            assert content.summary == "新简介"
            assert (content.year, content.media, content.episodes) == (2021, "剧场版", 1)
            assert content.source_material == "原创"
        finally:
            db.close()

    def test_invalid_objective_fields_are_rejected(self, sub_env):
        """客观信息也要校验：年份越界 / 媒介不在取值内都回 400 且不落库。"""
        login_as_user(sub_env.client)
        bad_year = _submit_create(sub_env.client, title="年份越界", year="1899")
        assert bad_year.status_code == 400
        bad_media = _submit_create(sub_env.client, title="媒介乱填", media="电视剧")
        assert bad_media.status_code == 400
        assert _pending(sub_env.session_factory) == []


class TestDuplicateTitle:
    """同名作品直接拦住，避免审核通过后分裂出两个详情页。"""

    def test_create_with_existing_title_is_rejected(self, sub_env):
        db = sub_env.session_factory()
        try:
            existing = crud.get_content(db, 1).title
        finally:
            db.close()

        login_as_user(sub_env.client)
        resp = _submit_create(sub_env.client, title=existing)
        assert resp.status_code == 400
        assert "已有同名作品" in resp.text
        assert _pending(sub_env.session_factory) == []

    def test_duplicate_check_ignores_case_and_spacing(self, sub_env):
        db = sub_env.session_factory()
        try:
            existing = crud.get_content(db, 1).title
        finally:
            db.close()

        login_as_user(sub_env.client)
        resp = _submit_create(sub_env.client, title=f"  {existing.upper()}  ")
        assert resp.status_code == 400


class TestReviewDiff:
    """审核页要把新旧差异直接算出来，并显示提交人账号名。"""

    def test_review_page_shows_diff_and_username(self, sub_env):
        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            content.year = 2021
            db.commit()
            old_title = content.title
        finally:
            db.close()

        login_as_user(sub_env.client)
        sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（改）",
                "type": "anime",
                "summary": "新简介",
                "year": "2022",
            },
            follow_redirects=False,
        )
        login_as_admin(sub_env.client)
        page = sub_env.client.get("/manage/submissions?status=pending").text
        assert "星海航线（改）" in page
        assert "2021" in page and "2022" in page
        assert old_title in page
        assert USER_USERNAME in page
        assert "用户 #" not in page

    def test_diff_kept_field_is_not_shown_as_change(self, sub_env):
        """留空的客观字段不该在对比里显示成「改成空」。"""
        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            content.media = "TV"
            db.commit()
        finally:
            db.close()

        login_as_user(sub_env.client)
        sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（改）",
                "type": "anime",
                "summary": "新简介",
                "media": "",
            },
            follow_redirects=False,
        )
        row = _pending(sub_env.session_factory)[0]
        db = sub_env.session_factory()
        try:
            diff = {
                item["label"]: item
                for item in crud.submission_diff(
                    crud.get_content_submission(db, row.id), crud.get_content(db, 1)
                )
            }
            assert diff["媒介"]["kept"] is True
            assert diff["媒介"]["changed"] is False
            assert diff["标题"]["changed"] is True
        finally:
            db.close()


class TestRatingNotUserEditable:
    """评分不是用户可填字段：只能来自外部数据源（Bangumi 等）的统计。

    允许申请人在「新增 / 修改作品」里随手填一个分数，等于让未经统计的
    个人数字通过审核落进内容库，还会被详情页当作站点评分展示。
    """

    def test_form_has_no_rating_input(self, sub_env):
        """表单不再渲染评分输入框。"""
        login_as_user(sub_env.client)
        page = sub_env.client.get("/submit").text
        assert 'name="rating"' not in page
        assert "本流程不收集评分" in page

    def test_rating_in_payload_is_ignored(self, sub_env):
        """即便 payload 里塞 rating，服务端也不接收：不校验、不落库。"""
        login_as_user(sub_env.client)
        resp = _submit_create(sub_env.client, title="无评分新作", rating="99")
        assert resp.status_code == 303
        row = _pending(sub_env.session_factory)[0]
        # 新增申请的快照评分是 0.0（暂无），而不是用户塞的 99
        assert row.rating == 0.0

    def test_create_approves_without_rating(self, sub_env):
        """通过审核的新增作品评分保持 0.0，页面显示「暂无评分」。"""
        login_as_user(sub_env.client)
        _submit_create(sub_env.client, title="无评分新作")
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        db = sub_env.session_factory()
        try:
            content = crud.find_content_by_title(db, "无评分新作")
            assert content is not None
            assert content.rating == 0.0
        finally:
            db.close()

        login_as_user(sub_env.client)
        page = sub_env.client.get(f"/contents/{content.id}").text
        assert "暂无评分" in page

    def test_update_keeps_external_rating(self, sub_env):
        """修改申请通过后，作品的外部数据源评分原样保留。"""
        db = sub_env.session_factory()
        try:
            before = crud.get_content(db, 1).rating
        finally:
            db.close()

        login_as_user(sub_env.client)
        _submit_create(sub_env.client, title="占位")  # noqa: F841 - 制造 pending 无需使用
        sub_env.client.post(
            "/contents/1/edit",
            data={"title": "星海航线（改）", "type": "anime", "summary": "新简介"},
            follow_redirects=False,
        )
        rows = _pending(sub_env.session_factory)
        row = next(r for r in rows if r.kind == crud.SUBMISSION_KIND_UPDATE)

        login_as_admin(sub_env.client)
        sub_env.client.post(
            f"/manage/submissions/{row.id}/approve", follow_redirects=False
        )
        db = sub_env.session_factory()
        try:
            assert crud.get_content(db, 1).rating == before
        finally:
            db.close()
