"""词条申请：登录用户提交新增 / 修改 → 管理员审核 → 通过后写入内容库。

覆盖：
- 访问控制：游客跳登录、用户可开表单、后台审核页仅管理员；
- 提交新增：进入 pending，审核前不写入 contents；
- 字段校验：空标题 / 非法类型 / 评分越界都回 400 且不落库；
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
        "summary": "用于词条申请测试的简介。",
        "rating": "8.5",
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
        assert "申请新增词条" in resp.text
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

        bad_rating = _submit_create(sub_env.client, rating="99")
        assert bad_rating.status_code == 400
        assert "评分需在 0 到 10 之间" in bad_rating.text

        assert _pending(sub_env.session_factory) == []

    def test_update_submission_does_not_touch_content(self, sub_env):
        login_as_user(sub_env.client)
        resp = sub_env.client.post(
            "/contents/1/edit",
            data={
                "title": "星海航线（改）",
                "type": "anime",
                "summary": "修改后的简介",
                "rating": "9.0",
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
        assert 'value="8.7"' in page.text


class TestAdminReview:
    def test_admin_page_requires_admin(self, sub_env):
        assert sub_env.client.get("/admin/submissions").status_code == 303  # 游客
        login_as_user(sub_env.client)
        assert sub_env.client.get("/admin/submissions").status_code == 403
        login_as_admin(sub_env.client)
        assert sub_env.client.get("/admin/submissions").status_code == 200

    def test_approve_create_writes_content(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        page = sub_env.client.get("/admin/submissions?status=pending")
        assert "新词条测试" in page.text

        resp = sub_env.client.post(
            f"/admin/submissions/{row.id}/approve", follow_redirects=False
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
                "rating": "9.5",
            },
        )
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        resp = sub_env.client.post(
            f"/admin/submissions/{row.id}/approve", follow_redirects=False
        )
        assert resp.status_code == 303

        db = sub_env.session_factory()
        try:
            content = crud.get_content(db, 1)
            assert content.title == "星海航线（新）"
            assert content.summary == "新简介"
            assert content.rating == 9.5
        finally:
            db.close()

    def test_reject_keeps_content_and_records_note(self, sub_env):
        login_as_user(sub_env.client)
        _submit_create(sub_env.client)
        row = _pending(sub_env.session_factory)[0]

        login_as_admin(sub_env.client)
        resp = sub_env.client.post(
            f"/admin/submissions/{row.id}/reject",
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
            f"/admin/submissions/{row.id}/approve", follow_redirects=False
        )
        assert "decided=ok" in first.headers["location"]
        second = sub_env.client.post(
            f"/admin/submissions/{row.id}/approve", follow_redirects=False
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
            f"/admin/submissions/{row.id}/approve", follow_redirects=False
        )
        assert resp.status_code == 403
