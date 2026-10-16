"""数据管理页面路由（测试管理平台 → 数据管理）。

面向管理员的数据维护入口，管理内容平台的三类数据：

- 作品数据（contents）：查看 / 删除 / 批量删除；
- 资讯数据（articles）：查看 / 删除；
- 新番档期（anime_schedules）：查看 / 删除 / 重新同步；
- 数据同步（本阶段新增）：选择数据源与范围 → 预览 → 确认写入 → 查看结果与历史。

设计约束：
- 全部页面挂在 /admin/data 下，由 app.main 用 require_admin_page 统一守卫，
  因此这里不重复鉴权：未登录会跳登录页，已登录但不是管理员会得到 403 页面；
- 删除是单表的真实写库操作，只删除所提交的主键，不做级联清理，
  也不触碰其它模块的数据（与既有测试平台 / 内容平台互不影响）；
- 「重新同步」复用既有同步流程（scripts/sync_yuc_schedule.run_sync），
  不复制也不修改同步逻辑；外部网络失败时把原因回显到页面，应用其它功能不受影响；
- 「数据同步」页同样只做编排：三类数据源的实际同步逻辑集中在 app.sync_tasks，
  该模块调用既有脚本/流程，本文件只负责表单、预览/确认两阶段与结果展示。
"""

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud, sync_tasks
from app.business_pages import CONTENT_TYPE_LABELS, WEEKDAY_NAMES
from app.database import SessionLocal
from app.page_common import DbSession, render_missing, templates
from scripts.sync_yuc_schedule import SeasonNotFoundError, YucFetchError, run_sync

router = APIRouter(tags=["admin-data"])

# 管理列表每页条数：内容量不大，固定 20 条便于逐页核对与批量勾选。
PAGE_SIZE = 20

# 同步历史展示条数：够看清最近几次同步即可，避免页面过长。
SYNC_HISTORY_LIMIT = 20

# 重新同步失败原因的展示上限：过长信息截断，避免撑爆地址栏与页面。
RESYNC_REASON_MAX = 300


# ---------- 小工具 ----------


def _pages(total: int, page_size: int = PAGE_SIZE) -> int:
    """总页数（至少 1，便于模板统一渲染分页）。"""
    return max(1, math.ceil(total / page_size)) if total else 1


def _url(path: str, **params) -> str:
    """拼装带查询参数的站内地址；空字符串参数自动忽略。"""
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"{path}?{urlencode(clean)}" if clean else path


def _page_param(value) -> str | None:
    """页码回传：第 1 页不写入地址，保持 URL 简洁。"""
    text = str(value).strip()
    return None if text in ("", "0", "1") else text


def _form_text(form, key: str) -> str:
    """取表单里的单个文本字段（不存在时返回空串）。"""
    return str(form.get(key, "")).strip()


def _collect_ids(values) -> list[int]:
    """把表单里的 ids 原始值转成合法主键列表；非数字项直接忽略。"""
    ids: list[int] = []
    for value in values:
        text = str(value).strip()
        if text.isdigit():
            ids.append(int(text))
    return ids


def _short(text: str, limit: int = RESYNC_REASON_MAX) -> str:
    """折叠空白并截断过长提示。"""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _clean_content_type(value: str) -> str | None:
    """页面侧宽容处理非法类型：忽略该筛选条件（严格校验由 API 负责）。"""
    content_type = (value or "").strip().lower()
    return content_type if content_type in CONTENT_TYPE_LABELS else None


def resync_season(season_code: str):
    """按档期重新同步：复用既有同步流程，正式写入（apply=True）。

    单独抽出为一个函数，便于测试替换（不触网、不写真实库）。
    """
    return run_sync(season_code, session_factory=SessionLocal, apply=True)


def run_sync_task(
    task_key: str, scope: str, *, apply: bool, scope_limit: str | None = None,
    actor_id: int | None = None,
):
    """执行一次「数据同步」任务（预览或正式）。

    与 resync_season 一样单独抽出，便于测试替换为不触网、不写真实库的桩函数。
    """
    return sync_tasks.run_task(
        task_key,
        scope,
        apply=apply,
        scope_limit=scope_limit,
        actor_id=actor_id,
    )


# ---------- 数据管理总览 ----------


@router.get("/admin/data", response_class=HTMLResponse)
def data_overview(request: Request, db: DbSession):
    """数据管理总览：三类数据的当前规模与各自入口。"""
    return templates.TemplateResponse(
        request=request,
        name="admin_data.html",
        context={
            "content_total": crud.count_contents(db),
            "article_total": crud.count_articles(db),
            "schedule_total": crud.count_schedules(db),
            "season_total": crud.count_schedule_seasons(db),
        },
    )


# ---------- 作品数据 ----------


@router.get("/admin/data/contents", response_class=HTMLResponse)
def contents_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    content_type: str = Query(default="", alias="type"),
    page: int = Query(default=1, ge=1),
    deleted: int | None = Query(default=None),
):
    """作品数据：分页列表 + 关键词/类型筛选；删除结果由 deleted 参数回显。"""
    keyword = q.strip()
    ctype = _clean_content_type(content_type)
    total, contents = crud.search_contents_page(
        db, keyword=keyword, content_type=ctype, page=page, page_size=PAGE_SIZE
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_contents.html",
        context={
            "contents": contents,
            "total": total,
            "page": page,
            "total_pages": _pages(total),
            "q": q,
            "content_type": ctype or "",
            "type_labels": CONTENT_TYPE_LABELS,
            "deleted": deleted,
        },
    )


@router.post("/admin/data/contents/delete")
async def delete_contents_page(request: Request, db: DbSession):
    """删除作品：同一入口同时支持单条与批量删除。

    表单以重复的 ids 字段提交选中主键（单条删除按钮携带一个 id，
    批量删除按钮配合勾选框携带多个 id），这里用 form.getlist 全部取出。
    """
    form = await request.form()
    ids = _collect_ids(form.getlist("ids"))
    deleted = crud.delete_contents(db, ids)
    return RedirectResponse(
        _url(
            "/admin/data/contents",
            q=_form_text(form, "q"),
            type=_form_text(form, "type"),
            page=_page_param(_form_text(form, "page")),
            deleted=deleted,
        ),
        status_code=303,
    )


# ---------- 资讯数据 ----------


@router.get("/admin/data/articles", response_class=HTMLResponse)
def articles_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    deleted: int | None = Query(default=None),
):
    """资讯数据：分页列表 + 关键词筛选（含未发布文章，便于管理核对）。"""
    keyword = q.strip()
    total, articles = crud.search_articles_page(
        db, keyword=keyword, page=page, page_size=PAGE_SIZE, published_only=False
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_articles.html",
        context={
            "articles": articles,
            "total": total,
            "page": page,
            "total_pages": _pages(total),
            "q": q,
            "deleted": deleted,
        },
    )


@router.post("/admin/data/articles/{article_id}/delete")
def delete_article_page(
    request: Request,
    article_id: int,
    db: DbSession,
    q: str = Form(""),
    page: str = Form("1"),
):
    """删除单篇资讯；文章不存在返回 404 页面。"""
    article = crud.get_article(db, article_id, published_only=False)
    if article is None:
        return render_missing(request, "资讯数据", article_id)
    deleted = crud.delete_articles(db, [article_id])
    return RedirectResponse(
        _url("/admin/data/articles", q=q.strip(), page=_page_param(page), deleted=deleted),
        status_code=303,
    )


# ---------- 新番档期 ----------


@router.get("/admin/data/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request,
    db: DbSession,
    season: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    deleted: int | None = Query(default=None),
    resync: str = Query(default=""),
    added: int = Query(default=0),
    updated: int = Query(default=0),
    unchanged: int = Query(default=0),
    failed: int = Query(default=0),
    reason: str = Query(default=""),
):
    """新番档期：分页列表 + 档期/关键词筛选；删除与重新同步结果均由参数回显。"""
    seasons = crud.list_schedule_seasons(db)
    season_code = season.strip() or None
    keyword = q.strip() or None
    total, works = crud.list_schedules(
        db, season=season_code, keyword=keyword, page=page, page_size=PAGE_SIZE
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_schedules.html",
        context={
            "works": works,
            "total": total,
            "page": page,
            "total_pages": _pages(total),
            "seasons": seasons,
            "current_season": season_code,
            "q": q,
            "weekday_names": WEEKDAY_NAMES,
            "deleted": deleted,
            "resync": resync,
            "resync_added": added,
            "resync_updated": updated,
            "resync_unchanged": unchanged,
            "resync_failed": failed,
            "resync_reason": reason,
        },
    )


@router.post("/admin/data/schedules/{schedule_id}/delete")
def delete_schedule_page(
    request: Request,
    schedule_id: int,
    db: DbSession,
    season: str = Form(""),
    q: str = Form(""),
    page: str = Form("1"),
):
    """删除单条档期作品；不存在返回 404 页面。

    只删除本地记录，不改动同步脚本；之后可对该档期执行「重新同步」再写回。
    """
    record = crud.get_schedule(db, schedule_id)
    if record is None:
        return render_missing(request, "档期作品", schedule_id)
    deleted = crud.delete_schedules(db, [schedule_id])
    return RedirectResponse(
        _url(
            "/admin/data/schedules",
            season=season.strip(),
            q=q.strip(),
            page=_page_param(page),
            deleted=deleted,
        ),
        status_code=303,
    )


@router.post("/admin/data/schedules/resync")
def resync_schedule_page(
    season_code: str = Form(""),
    season: str = Form(""),
    q: str = Form(""),
    page: str = Form("1"),
):
    """按档期重新同步：调用既有 Yuc 同步流程，把结果或失败原因回显到页面。

    成功时回显本次新增 / 更新 / 不变 / 失败计数；网络不可用或档期不存在时
    只回显原因，不抛异常、不影响任何其它功能。
    """
    code = season_code.strip()
    base = {"season": season.strip(), "q": q.strip(), "page": _page_param(page)}
    if not code:
        return RedirectResponse(
            _url("/admin/data/schedules", **base, resync="error", reason="缺少档期参数"),
            status_code=303,
        )
    try:
        summary = resync_season(code)
    except (YucFetchError, SeasonNotFoundError) as exc:
        return RedirectResponse(
            _url("/admin/data/schedules", **base, resync="error", reason=_short(str(exc))),
            status_code=303,
        )
    except Exception as exc:  # 同步过程中的意外错误同样只回显，不中断页面
        return RedirectResponse(
            _url(
                "/admin/data/schedules",
                **base,
                resync="error",
                reason=_short(f"同步失败：{exc}"),
            ),
            status_code=303,
        )
    return RedirectResponse(
        _url(
            "/admin/data/schedules",
            **base,
            resync="ok",
            added=summary.added,
            updated=summary.updated,
            unchanged=summary.unchanged,
            failed=summary.failed,
        ),
        status_code=303,
    )


# ---------- 数据同步（选择数据源与范围 → 预览 → 确认写入） ----------


def _actor_id(request: Request) -> int | None:
    """当前登录管理员的主键（用于运行记录审计）；未知时返回 None。"""
    return getattr(getattr(request.state, "current_user", None), "id", None)


def _render_sync_page(
    request: Request,
    db,
    task_key: str,
    *,
    result=None,
    error: str = "",
    scope: str = "",
    limit: str = "",
    can_confirm: bool = False,
):
    """渲染数据同步页面。

    result 既可以是刚执行完的 TaskResult（预览后的结果），
    也可以是历史记录 SyncTaskRun（?run=<id> 查看某次结果），
    两者字段名一致，模板可共用同一套渲染逻辑。

    can_confirm 只在「刚刚完成一次预览」时为真：确认表单需要把预览时提交的
    原始范围文本原样回传，因此只有预览流程知道它，历史回看不提供确认按钮。
    """
    spec = sync_tasks.get_task(task_key) or sync_tasks.TASKS[0]
    total, runs = crud.list_sync_task_runs(db, limit=SYNC_HISTORY_LIMIT)
    return templates.TemplateResponse(
        request=request,
        name="admin_sync.html",
        context={
            "spec": spec,
            "tasks": sync_tasks.TASKS,
            "task_labels": sync_tasks.task_labels(),
            "seasons": crud.list_schedule_seasons(db),
            "categories": sync_tasks.article_categories(),
            "result": result,
            "can_confirm": can_confirm,
            "error": error,
            "form_scope": scope,
            "form_limit": limit,
            "runs": runs,
            "runs_total": total,
            "status_labels": sync_tasks.STATUS_LABELS,
            "status_badges": sync_tasks.STATUS_BADGES,
            "mode_labels": sync_tasks.MODE_LABELS,
            "mode_apply": sync_tasks.MODE_APPLY,
            "detail_labels": sync_tasks.DETAIL_LABELS,
            "max_bangumi_ids": sync_tasks.MAX_BANGUMI_IDS,
            "max_article_limit": sync_tasks.MAX_ARTICLE_LIMIT,
        },
    )


@router.get("/admin/data/sync", response_class=HTMLResponse)
def sync_page(
    request: Request,
    db: DbSession,
    task: str = Query(default=sync_tasks.TASK_BANGUMI),
    run: int | None = Query(default=None),
    error: str = Query(default=""),
):
    """数据同步页：数据源表单（按 task 切换）+ 结果面板 + 同步历史。

    ?run=<id> 用于查看某次（预览或正式）同步的结果快照。
    """
    selected = crud.get_sync_task_run(db, run) if run else None
    task_key = selected.task_key if selected is not None else task
    return _render_sync_page(
        request, db, task_key, result=selected, error=_short(error) if error else ""
    )


@router.post("/admin/data/sync/preview", response_class=HTMLResponse)
def sync_preview_page(
    request: Request,
    db: DbSession,
    task: str = Form(""),
    scope: str = Form(""),
    limit: str = Form(""),
):
    """预览：只读源数据并汇总「可写入什么」，不写任何业务数据。

    范围不合法、同数据源已有任务在执行、网络失败等情况一律原页回显原因，
    不抛 500；预览结果会附上确认按钮（确认后按同一范围正式写入）。
    """
    task_key = task.strip() or sync_tasks.TASK_BANGUMI
    try:
        result = run_sync_task(
            task_key, scope, apply=False, scope_limit=limit or None,
            actor_id=_actor_id(request),
        )
    except (sync_tasks.ScopeError, sync_tasks.TaskBusyError) as exc:
        return _render_sync_page(
            request, db, task_key, error=str(exc), scope=scope, limit=limit
        )
    except Exception as exc:
        return _render_sync_page(
            request, db, task_key, error=f"预览失败：{_short(str(exc))}",
            scope=scope, limit=limit,
        )
    return _render_sync_page(
        request, db, task_key, result=result, scope=scope, limit=limit, can_confirm=True
    )


@router.post("/admin/data/sync/apply")
def sync_apply_page(
    request: Request,
    task: str = Form(""),
    scope: str = Form(""),
    limit: str = Form(""),
):
    """确认写入：按预览时的同一范围正式同步，随后重定向查看结果。

    重复提交不会产生重复数据（由各同步流程自身的幂等规则保证）；
    结果与失败原因都记录在 sync_task_runs，重定向后从库里读出来展示。
    """
    task_key = task.strip() or sync_tasks.TASK_BANGUMI
    base = {"task": task_key}
    try:
        result = run_sync_task(
            task_key, scope, apply=True, scope_limit=limit or None,
            actor_id=_actor_id(request),
        )
    except (sync_tasks.ScopeError, sync_tasks.TaskBusyError) as exc:
        return RedirectResponse(
            _url("/admin/data/sync", **base, error=_short(str(exc))), status_code=303
        )
    except Exception as exc:
        return RedirectResponse(
            _url("/admin/data/sync", **base, error=_short(f"同步未完成：{exc}")),
            status_code=303,
        )
    if result.run_id is not None:
        return RedirectResponse(
            _url("/admin/data/sync", **base, run=result.run_id), status_code=303
        )
    return RedirectResponse(_url("/admin/data/sync", **base), status_code=303)
