"""首页海报轮播管理页（内容平台 → 内容管理，/manage/posters，仅 staff）。

管「首页海报全站名单」的三种更新模式（与页面内手动浏览是两个独立功能）：
- 关闭自动更新（默认）：定时任务不跑，保留当前名单与排序；
- 自动更新：每日 / 每周从当季新番候选轮转生成新名单，整批生效；
- 手动指定：管理员从**全站作品**里挑（支持标题 / 日文原名 / ID 搜索、
  类型筛选、分页浏览），逐个加入或整页加入，排序号在已选名单里调。

路由级守卫由 app.main 统一挂 require_admin_page（与 /manage/* 其余页面一致），
普通用户与游客都到不了这些端点。表单写操作统一 PRG：303 + 查询参数回显。
"""

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select

from app import home_posters
from app.admin_common import page_param
from app.admin_common import url_with as _url
from app.business_pages import CONTENT_TYPE_LABELS
from app.home_posters import (
    FREQ_LABELS,
    MODE_AUTO,
    MODE_LABELS,
)
from app.models import Content
from app.page_common import DbSession, templates

router = APIRouter(tags=["poster-admin"])

# 手动候选池每页条数：一屏放得下、带封面缩略图也不重。
MANUAL_PAGE_SIZE = 48


def search_manual_candidates(
    db: DbSession,
    *,
    keyword: str = "",
    content_type: str = "",
    page: int = 1,
) -> tuple[int, list[dict], int]:
    """全站带封面作品的管理页候选池（与首页当季轮转候选是两回事）。

    手动指定不再局限于「当季新番」：管理员从全站作品里挑，按标题 /
    日文原名搜索，或直接输作品 ID；类型可选。分页浏览，每页
    ``MANUAL_PAGE_SIZE`` 条，评分降序（同分按 id，保证翻页顺序稳定）。
    返回 (命中总数, 当前页候选, 总页数)。
    """
    stmt = select(Content).where(Content.image_url.is_not(None), Content.image_url != "")
    kw = (keyword or "").strip()
    if kw:
        if kw.isdigit():
            # 纯数字按作品 ID 精确找（候选池里定位单部作品最快的方式）
            stmt = stmt.where(Content.id == int(kw))
        else:
            like = f"%{kw}%"
            stmt = stmt.where(
                or_(Content.title.like(like), Content.title_original.like(like))
            )
    if content_type in CONTENT_TYPE_LABELS:
        stmt = stmt.where(Content.type == content_type)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = (total + MANUAL_PAGE_SIZE - 1) // MANUAL_PAGE_SIZE if total else 1
    page = min(max(page, 1), pages)
    rows = db.scalars(
        stmt.order_by(Content.rating.desc(), Content.id)
        .offset((page - 1) * MANUAL_PAGE_SIZE)
        .limit(MANUAL_PAGE_SIZE)
    ).all()
    items = [
        {
            "content_id": row.id,
            "title": row.title,
            "image_url": row.image_url,
            "rating": row.rating or 0.0,
            "type": row.type,
            "href": f"/contents/{row.id}",
        }
        for row in rows
    ]
    return total, items, pages


def _active_content_ids(db: DbSession) -> list[int]:
    """当前生效名单的 content_id（保序）。"""
    return [
        item["content_id"]
        for item in home_posters.list_active_posters(db)
        if item["content_id"]
    ]


@router.get("/manage/posters", response_class=HTMLResponse)
def posters_page(
    request: Request,
    db: DbSession,
    saved: str = Query(default=""),
    error: str = Query(default=""),
    ran: str = Query(default=""),
    ran_count: int = Query(default=0, ge=0),
    added: str = Query(default=""),
    removed: str = Query(default=""),
    notice: str = Query(default=""),
    q: str = Query(default=""),
    type: str = Query(default="", alias="type"),
    page: int = Query(default=1, ge=1),
):
    """海报管理页：更新模式 / 频率、手动指定（全站搜索）、当前生效名单、立即执行。"""
    config = home_posters.get_config(db)
    # 手动候选池：全站带封面作品，支持搜索 / 类型筛选 / 分页。
    candidate_total, candidates, candidate_pages = search_manual_candidates(
        db, keyword=q, content_type=type, page=page
    )
    active = home_posters.list_active_posters(db)
    return templates.TemplateResponse(
        request=request,
        name="admin_posters.html",
        context={
            "config": config,
            "mode_labels": MODE_LABELS,
            "freq_labels": FREQ_LABELS,
            "candidates": candidates,
            "candidate_total": candidate_total,
            "candidate_pages": candidate_pages,
            "candidate_page": page,
            "keyword": q,
            "type_filter": type,
            "active_posters": active,
            "type_labels": CONTENT_TYPE_LABELS,
            "saved": saved,
            "error": error,
            "ran": ran,
            "ran_count": ran_count,
            "added": added,
            "removed": removed,
            "notice": notice,
        },
    )


@router.post("/manage/posters/mode")
def save_mode(
    db: DbSession,
    mode: str = Form(default=""),
    frequency: str = Form(default=""),
):
    """保存更新模式 / 频率；切到自动更新时立即执行一次（让首页马上换新名单）。"""
    if mode not in MODE_LABELS:
        return RedirectResponse(_url("/manage/posters", error="未知的更新模式"), status_code=303)
    config = home_posters.set_mode(db, mode=mode, frequency=frequency)
    if config.mode == MODE_AUTO:
        # 启用即执行一次：不必等下一个 cron 窗口，首页当场生效；失败保留旧名单。
        ok, message = home_posters.run_scheduled(db, force=True)
        if not ok:
            return RedirectResponse(
                _url("/manage/posters", error=f"已切换为自动更新，但本次执行失败：{message}"),
                status_code=303,
            )
        return RedirectResponse(
            _url("/manage/posters", saved="auto", ran="1"), status_code=303
        )
    return RedirectResponse(_url("/manage/posters", saved=mode), status_code=303)


@router.post("/manage/posters/manual")
async def save_manual(request: Request, db: DbSession):
    """保存已选名单：带 ``remove_id`` 时移除该部（允许清空），否则按排序号重排。

    表单里每行一个 hidden ``content_ids``（保序 = 当前名单顺序）+ 一个
    ``order_{id}`` 排序框；「移除」按钮以 ``remove_id`` 同名提交——
    与「保存排序」共用一个端点，靠按钮 name/value 区分动作。
    """
    form = await request.form()
    checked: list[int] = []
    orders: dict[int, int] = {}
    for key, value in form.items():
        if not key.startswith("order_"):
            continue
        try:
            content_id = int(key.removeprefix("order_"))
        except ValueError:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            orders[content_id] = int(text)
        except ValueError:
            continue
    for raw in form.getlist("content_ids"):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in checked:
            checked.append(value)
    # 移除分支：从当前名单里删掉指定的一部（删完 = 清空，首页回落实时候选）
    remove_raw = form.get("remove_id")
    if remove_raw not in (None, ""):
        try:
            remove_id = int(str(remove_raw).strip())
        except (TypeError, ValueError):
            return RedirectResponse(
                _url("/manage/posters", error="无效的作品 ID"), status_code=303
            )
        ranked = [cid for cid in checked if cid != remove_id]
        home_posters.set_manual_list(db, ranked)
        if not ranked:
            return RedirectResponse(
                _url(
                    "/manage/posters",
                    saved="manual",
                    notice="名单已清空：首页将展示当季新番实时候选",
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url("/manage/posters", saved="manual", removed="1"), status_code=303
        )
    # 排序分支：有排序号的在前（按号升序），其余按名单原序。
    ranked = sorted(checked, key=lambda cid: (orders.get(cid, 10**6), checked.index(cid)))
    count = home_posters.set_manual_list(db, ranked)
    if count == 0:
        return RedirectResponse(
            _url("/manage/posters", error="名单为空：请先从下方候选池加入作品"),
            status_code=303,
        )
    return RedirectResponse(_url("/manage/posters", saved="manual"), status_code=303)


def _back_url(form, **extra) -> str:
    """写操作后回到候选池原页（保留搜索词 / 类型 / 页码），可附提示参数。"""
    params = {
        "q": form.get("q") or "",
        "type": form.get("type") or "",
        "page": page_param(form.get("page")),
        **extra,
    }
    return _url("/manage/posters", **params)


@router.post("/manage/posters/add")
async def add_candidates(request: Request, db: DbSession):
    """把候选池里勾选的作品追加到名单末尾（模式自动切「手动指定」）。

    跨页累加：勾选状态不依赖单个大表单——翻页、搜索都不会丢已选，
    已选名单与候选浏览是两个独立区块。
    """
    form = await request.form()
    new_ids: list[int] = []
    for raw in form.getlist("content_ids"):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in new_ids:
            new_ids.append(value)
    if not new_ids:
        return RedirectResponse(
            _back_url(form, error="没有勾选任何作品"), status_code=303
        )
    existing = _active_content_ids(db)
    merged = existing + [cid for cid in new_ids if cid not in existing]
    count = home_posters.set_manual_list(db, merged)
    if count == 0:
        return RedirectResponse(
            _back_url(form, error="没有选中任何有效作品（需要有封面的站内作品）"),
            status_code=303,
        )
    return RedirectResponse(
        _back_url(form, saved="manual", added=str(len(new_ids))), status_code=303
    )


@router.post("/manage/posters/run-now")
def run_now(db: DbSession):
    """管理员主动执行一次自动更新（结果经 build_auto_update 的有效性检查）。"""
    ok, message = home_posters.run_scheduled(db, force=True)
    if not ok:
        return RedirectResponse(_url("/manage/posters", error=message), status_code=303)
    count = home_posters.get_config(db).last_count
    return RedirectResponse(
        _url("/manage/posters", ran="1", ran_count=count), status_code=303
    )
