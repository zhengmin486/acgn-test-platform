"""个人空间页面路由：用户信息 / 简介编辑 / 作品收藏 / 浏览记录。

边界与约定：
- 整个模块只要求登录（不限制角色）：路由级挂 ``require_login_page``，
  未登录抛 ``LoginRequired``，由异常处理器 303 跳 ``/login?next=...``，
  普通用户与管理员一视同仁；``_current_account`` 只负责取账号 ORM 对象；
- 收藏与浏览记录全部按 ``user_id`` 查询，只归属当前登录账号，不做任何跨账号展示；
- 写操作统一 PRG：303 跳回 ``/me``（或来源详情页）并用查询参数回显结果，
  避免刷新重复提交；
- 页面布局沿用内容平台（base_content.html），个人空间是内容平台的登录用户功能。
"""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud
from app.auth import LoginRequired, require_login_page, safe_next_url
from app.business_pages import CONTENT_TYPE_LABELS
from app.page_common import DbSession, render_missing, templates

# 路由级守卫：整个个人空间只要求登录（不限制角色）。
# 未登录 -> LoginRequired -> 303 跳 /login?next=...；普通用户与管理员一视同仁。
router = APIRouter(tags=["pages"], dependencies=[Depends(require_login_page)])

# 个人空间两个列表共用分页大小（收藏卡片 / 浏览记录表格）。
SPACE_PAGE_SIZE = 10

# Tab 取值：非法值回退到收藏（与首页「宽容忽略」风格一致）。
SPACE_TABS = ("favorites", "history")


def _current_account(request: Request, db: DbSession):
    """取当前登录账号的 ORM 对象。

    登录状态由路由级 ``require_login_page`` 保证；这里只处理
    「会话有效但账号已被删除」的残余情况——同样按未登录处理。
    """
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=request.url.path)
    account = crud.get_user(db, current_user.id)
    if account is None:
        raise LoginRequired(next_url=request.url.path)
    return account


@router.get("/me", response_class=HTMLResponse)
def space_page(
    request: Request,
    db: DbSession,
    tab: str = Query(default="favorites", max_length=20),
    page: int = Query(default=1, ge=1),
):
    """个人空间：用户信息 + 简介编辑 + 作品收藏 / 浏览记录（Tab 切换 + 分页）。"""
    account = _current_account(request, db)
    if tab not in SPACE_TABS:
        tab = "favorites"

    favorites_total, favorites = (0, [])
    history_total, history = (0, [])
    if tab == "favorites":
        favorites_total, favorites = crud.list_favorites(
            db, account.id, page=page, page_size=SPACE_PAGE_SIZE
        )
    else:
        history_total, history = crud.list_view_history(
            db, account.id, page=page, page_size=SPACE_PAGE_SIZE
        )

    active_total = favorites_total if tab == "favorites" else history_total
    total_pages = max(1, (active_total + SPACE_PAGE_SIZE - 1) // SPACE_PAGE_SIZE)

    return templates.TemplateResponse(
        request=request,
        name="space.html",
        context={
            "account": account,
            "tab": tab,
            "page": page,
            "total_pages": total_pages,
            "type_labels": CONTENT_TYPE_LABELS,
            "favorites": favorites,
            "favorites_total": favorites_total,
            "favorites_count": crud.count_favorites(db, account.id),
            "history": history,
            "history_total": history_total,
            "history_count": crud.count_view_history(db, account.id),
            "bio_max_length": crud.MAX_BIO_LENGTH,
        },
    )


@router.post("/me/profile")
def update_profile(request: Request, db: DbSession, bio: str = Form(default="")):
    """保存简介：空串表示清除简介；超长只回显错误，不落库。"""
    account = _current_account(request, db)
    try:
        crud.set_user_bio(db, account, bio)
    except ValueError:
        return RedirectResponse("/me?bio=error", status_code=303)
    return RedirectResponse("/me?bio=ok", status_code=303)


@router.post("/contents/{content_id}/favorite")
def toggle_favorite(
    request: Request,
    content_id: int,
    db: DbSession,
    next: str = Form(default=""),
):
    """详情页收藏 / 取消收藏（切换）；作品不存在走统一 404。"""
    account = _current_account(request, db)
    if crud.get_content(db, content_id) is None:
        return render_missing(request, "内容", content_id)
    if crud.is_favorite(db, account.id, content_id):
        crud.remove_favorite(db, account.id, content_id)
        result = "removed"
    else:
        crud.add_favorite(db, account.id, content_id)
        result = "added"
    # next 经 safe_next_url 校验（只允许站内相对路径），拼 fav 回参后 303 回跳。
    target = safe_next_url(next) or f"/contents/{content_id}"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}fav={result}", status_code=303)


@router.post("/me/favorites/{content_id}/remove")
def remove_favorite(request: Request, content_id: int, db: DbSession):
    """个人空间内取消单条收藏；PRG 回收藏 Tab。"""
    account = _current_account(request, db)
    crud.remove_favorite(db, account.id, content_id)
    return RedirectResponse("/me?tab=favorites&fav_removed=1", status_code=303)


@router.post("/me/favorites/clear")
def clear_favorites(request: Request, db: DbSession):
    """清空全部收藏；回显删除行数。"""
    account = _current_account(request, db)
    removed = crud.clear_favorites(db, account.id)
    return RedirectResponse(f"/me?tab=favorites&fav_cleared={removed}", status_code=303)


@router.post("/me/history/{content_id}/remove")
def remove_history_item(request: Request, content_id: int, db: DbSession):
    """删除单条浏览记录；PRG 回浏览记录 Tab。"""
    account = _current_account(request, db)
    crud.remove_view_history(db, account.id, content_id)
    return RedirectResponse("/me?tab=history&history_removed=1", status_code=303)


@router.post("/me/history/clear")
def clear_history(request: Request, db: DbSession):
    """清空全部浏览记录；回显删除行数。"""
    account = _current_account(request, db)
    removed = crud.clear_view_history(db, account.id)
    return RedirectResponse(f"/me?tab=history&history_cleared={removed}", status_code=303)
