"""词条申请页面路由：登录用户提交新增 / 修改提案，等待管理员审核。

边界与约定：
- 路由级 require_login_page：未登录 303 跳 /login?next=...，普通用户与管理员一视同仁；
- 提交只写 content_submissions，不改 contents；审核通过后才落业务数据；
- 写操作统一 PRG：成功 303 跳个人空间「词条申请」Tab，失败回到表单并回显原因；
- 词条字段校验集中在 crud._normalize_content_fields，路由只负责读表单与渲染。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud
from app.auth import LoginRequired, require_login_page
from app.business_pages import CONTENT_TYPE_LABELS
from app.page_common import DbSession, render_missing, templates

router = APIRouter(tags=["pages"], dependencies=[Depends(require_login_page)])

SUBMISSION_REDIRECT = "/me?tab=submissions"


def _uid(request: Request) -> int:
    """取当前登录用户 id；路由级守卫已保证登录，这里处理账号被删除的残余情况。"""
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=request.url.path)
    return current_user.id


def _form_values(form=None, content=None) -> dict:
    """表单初值 / 回填值：来自已提交表单、既有作品或默认值。"""
    if form is not None:
        return {
            "title": str(form.get("title", "")),
            "type": str(form.get("type", "")),
            "summary": str(form.get("summary", "")),
            "rating": str(form.get("rating", "")),
        }
    if content is not None:
        return {
            "title": content.title,
            "type": content.type,
            "summary": content.summary,
            "rating": f"{content.rating:.1f}",
        }
    return {"title": "", "type": "anime", "summary": "", "rating": "8.0"}


def _render_form(request, *, mode, values, content_id=None, content=None, error=None):
    return templates.TemplateResponse(
        request=request,
        name="submission_form.html",
        context={
            "mode": mode,
            "values": values,
            "content_id": content_id,
            "content": content,
            "error": error,
            "type_labels": CONTENT_TYPE_LABELS,
        },
        status_code=400 if error else 200,
    )


@router.get("/submit", response_class=HTMLResponse)
def submit_page(request: Request):
    """新增词条申请表单。"""
    return _render_form(request, mode="create", values=_form_values())


@router.post("/submit", response_class=HTMLResponse)
async def submit_new(request: Request, db: DbSession):
    """提交新增词条申请；校验失败回填表单并给 400。"""
    uid = _uid(request)
    form = await request.form()
    values = _form_values(form)
    try:
        crud.submit_content(
            db,
            uid,
            crud.SUBMISSION_KIND_CREATE,
            values["title"],
            values["type"],
            values["summary"],
            values["rating"],
        )
    except ValueError as exc:
        return _render_form(request, mode="create", values=values, error=str(exc))
    return RedirectResponse(f"{SUBMISSION_REDIRECT}&submitted=create", status_code=303)


@router.get("/contents/{content_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, content_id: int, db: DbSession):
    """修改词条申请表单：预填当前作品内容。"""
    content = crud.get_content(db, content_id)
    if content is None:
        return render_missing(request, "内容", content_id)
    return _render_form(
        request,
        mode="update",
        values=_form_values(content=content),
        content_id=content_id,
        content=content,
    )


@router.post("/contents/{content_id}/edit", response_class=HTMLResponse)
async def edit_submit(request: Request, content_id: int, db: DbSession):
    """提交修改词条申请；目标作品不存在走统一 404。"""
    uid = _uid(request)
    content = crud.get_content(db, content_id)
    if content is None:
        return render_missing(request, "内容", content_id)
    form = await request.form()
    values = _form_values(form)
    try:
        crud.submit_content(
            db,
            uid,
            crud.SUBMISSION_KIND_UPDATE,
            values["title"],
            values["type"],
            values["summary"],
            values["rating"],
            content_id=content_id,
        )
    except ValueError as exc:
        return _render_form(
            request,
            mode="update",
            values=values,
            content_id=content_id,
            content=content,
            error=str(exc),
        )
    return RedirectResponse(f"{SUBMISSION_REDIRECT}&submitted=update", status_code=303)
