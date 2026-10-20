"""内容管理页面路由（内容平台 → 内容管理，/manage/*）。

面向管理员的内容运营入口：作品 / 资讯 / 新番档期 / 数据同步 / 同步历史 / 作品审核。
数据全部来自 app.crud 与 app.sync_tasks，不依赖测试平台的任何模块；
账号 / 邀请码 / 角色等站点治理在 app.site_admin_pages。

设计约束：
- 页面挂在 /manage 下，由 app.main 用 require_admin_page 统一守卫；
- 删除只按主键删除单表记录，不做级联，也不触碰其它模块的数据；
- 「重新同步」复用 scripts/sync_yuc_schedule.run_sync；「数据同步」只做编排，逻辑在 app.sync_tasks；
- 表单写操作统一 PRG：303 + 查询参数回显。
"""

import re
from datetime import datetime

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud, sync_tasks
from app.admin_common import (
    actor_id as _actor_id,
)
from app.admin_common import (
    collect_ids as _collect_ids,
)
from app.admin_common import (
    form_text as _form_text,
)
from app.admin_common import (
    page_count as _pages,
)
from app.admin_common import (
    page_param as _page_param,
)
from app.admin_common import (
    short_text as _short,
)
from app.admin_common import (
    url_with as _url,
)
from app.business_pages import CONTENT_TYPE_LABELS, WEEKDAY_NAMES
from app.database import SessionLocal
from app.page_common import DbSession, render_missing, templates
from scripts.import_bangumi import random_import, run_import
from scripts.sync_yuc_schedule import SeasonNotFoundError, YucFetchError, run_sync

router = APIRouter(tags=["content-admin"])

# 管理列表每页条数：内容量不大，固定 20 条便于逐页核对与批量勾选。
PAGE_SIZE = 20

# 同步历史展示条数：够看清最近几次同步即可，避免页面过长。
SYNC_HISTORY_LIMIT = 20

# 作品审核列表每页条数：与作品数据列表保持一致，便于逐页核对。
SUBMISSION_PAGE_SIZE = 20


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


def _relative_time(value: datetime | None, now: datetime | None = None) -> str:
    """相对时间文案：刚刚 / N 分钟前 / N 小时前 / N 天前（质量条用来标注数据新旧）。"""
    if value is None:
        return ""
    now = now or datetime.now()
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _quality_summary(batch, now: datetime | None = None):
    """质量条数据：最近一次回归批次的通过率与时间；没有批次返回 None。

    通过率口径与批次详情页一致：passed / total，空批次（total=0）为 None 而不是 0%。
    超过 24 小时的批次标记为「可能已过期」——批次是手动触发的，旧结果不代表当前代码。
    """
    if batch is None:
        return None
    now = now or datetime.now()
    finished = batch.finished_at or batch.started_at
    age = (now - finished).total_seconds() if finished else 0
    return {
        "batch": batch,
        "pass_rate": round(batch.passed / batch.total * 100, 1) if batch.total else None,
        "when": _relative_time(finished, now),
        "stale": age > 24 * 3600,
    }


@router.get("/manage", response_class=HTMLResponse)
def data_overview(request: Request, db: DbSession):
    """数据管理总览：三类数据的当前规模与各自入口 + 最近一次回归批次的质检条。"""
    batches = crud.list_batches(db)
    return templates.TemplateResponse(
        request=request,
        name="admin_data.html",
        context={
            "content_total": crud.count_contents(db),
            "article_total": crud.count_articles(db),
            "schedule_total": crud.count_schedules(db),
            "season_total": crud.count_schedule_seasons(db),
            "quality": _quality_summary(batches[0] if batches else None),
        },
    )


# ---------- 作品数据 ----------


@router.get("/manage/contents", response_class=HTMLResponse)
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


@router.post("/manage/contents/delete")
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
            "/manage/contents",
            q=_form_text(form, "q"),
            type=_form_text(form, "type"),
            page=_page_param(_form_text(form, "page")),
            deleted=deleted,
        ),
        status_code=303,
    )


# ---------- 关联管理（作品关系：作者 / 公司 / 系列） ----------

# 管理端可选的关联角色及默认实体类型：与 app/entity_links 的白名单一致，
# 公司类角色落 company，其余落 person。角色也可用「自定义」自由填写
# （Bangumi 信息卡的角色远不止这几种）。
ENTITY_ROLE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("原作", crud.ENTITY_TYPE_PERSON),
    ("插画", crud.ENTITY_TYPE_PERSON),
    ("导演", crud.ENTITY_TYPE_PERSON),
    ("音乐", crud.ENTITY_TYPE_PERSON),
    ("角色设计", crud.ENTITY_TYPE_PERSON),
    ("系列构成", crud.ENTITY_TYPE_PERSON),
    ("动画制作", crud.ENTITY_TYPE_COMPANY),
    ("出版社", crud.ENTITY_TYPE_COMPANY),
    ("发行", crud.ENTITY_TYPE_COMPANY),
)

# 操作结果回显码（PRG 后由查询参数带回，模板映射为中文提示）。
ENTITY_NOTES = {
    "added": "已添加作者/公司关联。",
    "exists": "该关联已存在，未重复添加。",
    "removed": "已删除作者/公司关联。",
    "link_missing": "关联不存在（可能已被删除）。",
    "content_missing": "作品不存在。",
    "series_saved": "已设置所属系列。",
    "series_cleared": "已清除所属系列。",
    "bad_input": "名称或角色为空，未做任何修改。",
}


@router.get("/manage/entities", response_class=HTMLResponse)
def entities_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    note: str = Query(default=""),
):
    """关联管理：按作品维护「作者/公司关联」与「所属系列」。

    自动识别（导入 / scripts/link_entities.py）覆盖不了的边角用例在这里
    手工补删：补一个信息卡没解析出来的作者、删一个错认的公司、把两部
    作品归入同一个系列。写操作统一 PRG（303 + 查询参数回显）。
    """
    keyword = q.strip()
    total, contents = crud.search_contents_page(
        db, keyword=keyword, page=page, page_size=PAGE_SIZE
    )
    rows = []
    for content in contents:
        rows.append(
            {
                "content": content,
                "links": crud.list_content_entity_links(db, content.id),
                "series": crud.get_series(db, content.series_id) if content.series_id else None,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="admin_entities.html",
        context={
            "rows": rows,
            "total": total,
            "page": page,
            "total_pages": _pages(total),
            "q": q,
            "note": note,
            "note_text": ENTITY_NOTES.get(note, ""),
            "role_options": ENTITY_ROLE_OPTIONS,
            "entity_type_labels": crud.ENTITY_TYPE_LABELS,
        },
    )


@router.post("/manage/entities/link/add")
def add_entity_link_page(
    request: Request,
    db: DbSession,
    content_id: int = Form(...),
    name: str = Form(""),
    role: str = Form(""),
    role_custom: str = Form(""),
    q: str = Form(""),
    page: str = Form("1"),
):
    """给指定作品手工补一条作者/公司关联；作品不存在返回 404 页面。"""
    content = crud.get_content(db, content_id)
    if content is None:
        return render_missing(request, "作品数据", content_id)
    final_role = (role_custom or "").strip() or role.strip()
    role_type = dict(ENTITY_ROLE_OPTIONS).get(role, crud.ENTITY_TYPE_PERSON)
    ok, _message = crud.add_content_entity_link(
        db, content_id, name, final_role, role_type
    )
    # 名称或角色为空时 add_content_entity_link 返回 False（bad_input）；
    # 已存在的组合它返回 True 并提示「已存在」，这里同样按 added 回显。
    note = "added" if ok else "bad_input"
    return RedirectResponse(
        _url(
            "/manage/entities",
            q=q,
            page=_page_param(page),
            note=note,
        ),
        status_code=303,
    )


@router.post("/manage/entities/link/delete")
def delete_entity_link_page(
    request: Request,
    db: DbSession,
    link_id: int = Form(...),
    q: str = Form(""),
    page: str = Form("1"),
):
    """删除一条作品-实体关联（手工纠错；关联不存在时仅提示，不报错）。"""
    if crud.delete_content_entity_link(db, link_id):
        note = "removed"
    else:
        note = "link_missing"
    return RedirectResponse(
        _url("/manage/entities", q=q, page=_page_param(page), note=note),
        status_code=303,
    )


@router.post("/manage/entities/series")
def set_content_series_page(
    request: Request,
    db: DbSession,
    content_id: int = Form(...),
    series: str = Form(""),
    action: str = Form("save"),
    q: str = Form(""),
    page: str = Form("1"),
):
    """设置 / 清除作品的所属系列；action=clear 或系列名为空都视为清除。"""
    content = crud.get_content(db, content_id)
    if content is None:
        return render_missing(request, "作品数据", content_id)
    final = crud.set_content_series(db, content_id, "" if action == "clear" else series)
    note = "series_saved" if final else "series_cleared"
    return RedirectResponse(
        _url("/manage/entities", q=q, page=_page_param(page), note=note),
        status_code=303,
    )


# ---------- 资讯数据 ----------


@router.get("/manage/articles", response_class=HTMLResponse)
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


@router.post("/manage/articles/{article_id}/delete")
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
        _url("/manage/articles", q=q.strip(), page=_page_param(page), deleted=deleted),
        status_code=303,
    )


# ---------- 新番档期 ----------


@router.get("/manage/schedules", response_class=HTMLResponse)
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


@router.post("/manage/schedules/{schedule_id}/delete")
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
            "/manage/schedules",
            season=season.strip(),
            q=q.strip(),
            page=_page_param(page),
            deleted=deleted,
        ),
        status_code=303,
    )


@router.post("/manage/schedules/resync")
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
            _url("/manage/schedules", **base, resync="error", reason="缺少档期参数"),
            status_code=303,
        )
    try:
        summary = resync_season(code)
    except (YucFetchError, SeasonNotFoundError) as exc:
        return RedirectResponse(
            _url("/manage/schedules", **base, resync="error", reason=_short(str(exc))),
            status_code=303,
        )
    except Exception as exc:  # 同步过程中的意外错误同样只回显，不中断页面
        return RedirectResponse(
            _url(
                "/manage/schedules",
                **base,
                resync="error",
                reason=_short(f"同步失败：{exc}"),
            ),
            status_code=303,
        )
    return RedirectResponse(
        _url(
            "/manage/schedules",
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
    seasons = crud.list_schedule_seasons(db)
    return templates.TemplateResponse(
        request=request,
        name="admin_sync.html",
        context={
            "spec": spec,
            "tasks": sync_tasks.TASKS,
            "task_labels": sync_tasks.task_labels(),
            "seasons": seasons,
            "season_suggestions": sync_tasks.suggest_season_codes(seasons),
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


@router.get("/manage/sync", response_class=HTMLResponse)
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


@router.post("/manage/sync/preview", response_class=HTMLResponse)
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


@router.post("/manage/sync/apply")
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
            _url("/manage/sync", **base, error=_short(str(exc))), status_code=303
        )
    except Exception as exc:
        return RedirectResponse(
            _url("/manage/sync", **base, error=_short(f"同步未完成：{exc}")),
            status_code=303,
        )
    if result.run_id is not None:
        return RedirectResponse(
            _url("/manage/sync", **base, run=result.run_id), status_code=303
        )
    return RedirectResponse(_url("/manage/sync", **base), status_code=303)


# ---------- 随机导入作品（随机偏移翻 Bangumi 排行榜页抽样导入） ----------

RANDOM_COUNT_DEFAULT = 20
RANDOM_COUNT_MAX = 100
RANDOM_TYPE_OPTIONS = (
    ("all", "动画 + 游戏"),
    ("anime", "仅动画"),
    ("game", "仅游戏"),
)


def _random_subject_types(rtype: str) -> tuple[int, ...]:
    """页面类型筛选 -> Bangumi 条目类型元组（与 TYPE_MAPPING 一致：2 动画 / 4 游戏）。"""
    rtype = (rtype or "all").strip().lower()
    if rtype == "anime":
        return (2,)
    if rtype == "game":
        return (4,)
    return (2, 4)


def _random_result_payload(
    summary, *, requested: int, sampled: list[int], mode: str,
    pages: int = 0, candidates: int = 0, duplicates: int = 0,
    nsfw_skipped: int = 0, rank_total: int | None = None,
) -> dict:
    """把 ImportSummary 拼成模板可读的结果字典（与 admin_sync.html 字段对齐）。

    pages / candidates / duplicates / nsfw_skipped / rank_total 是随机翻页统计：
    翻了几页、攒到多少新候选、本地跳过多少已入库与 nsfw，让命中情况一目了然。
    """
    return {
        "mode": mode,
        "requested": requested,
        "sampled": sampled,
        "pages": pages,
        "candidates": candidates,
        "dup_skipped": duplicates,
        "nsfw_skipped": nsfw_skipped,
        "rank_total": rank_total,
        "added": summary.imported,
        "preview": summary.preview,
        "duplicates": summary.duplicates,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "pending": summary.importable,
        "details": [
            {"status": o.status, "title": o.title, "reason": o.reason}
            for o in summary.outcomes
        ],
    }


@router.get("/manage/import-random", response_class=HTMLResponse)
def import_random_page(
    request: Request,
    db: DbSession,
    count: str = Query(default="20"),
    rtype: str = Query(default="all"),
    error: str = Query(default=""),
):
    """随机导入表单页：抓取条数（1-100 可调）+ 类型筛选。"""
    rtype = rtype if rtype in ("all", "anime", "game") else "all"
    try:
        count_val = max(1, min(RANDOM_COUNT_MAX, int(count)))
    except (TypeError, ValueError):
        count_val = RANDOM_COUNT_DEFAULT
    return templates.TemplateResponse(
        request=request,
        name="import_random.html",
        context={
            "count": count_val,
            "rtype": rtype,
            "type_options": RANDOM_TYPE_OPTIONS,
            "count_max": RANDOM_COUNT_MAX,
            "error": error,
            "result": None,
            "detail_labels": sync_tasks.DETAIL_LABELS,
        },
    )


@router.post("/manage/import-random", response_class=HTMLResponse)
def import_random_action(
    request: Request,
    db: DbSession,
    action: str = Form(default="preview"),
    count: str = Form(default="20"),
    rtype: str = Form(default="all"),
    ids: str = Form(default=""),
):
    """随机导入：预览（随机翻榜页 + 抽样 + 预映射，不写库）/ 确认写入（锁定预览 ids）。

    确认写入必须复用预览时抽样得到的 ids，否则重新随机会抽到不同的作品。
    """
    rtype = rtype if rtype in ("all", "anime", "game") else "all"
    if action == "apply":
        # 确认写入：使用预览时锁定的 ids，避免重新随机抽到不同作品
        id_tokens = [t for t in re.split(r"[\s,，、;；]+", ids or "") if t.isdigit()]
        sampled = [int(t) for t in id_tokens]
        if not sampled:
            return RedirectResponse(
                _url("/manage/import-random", error="缺少可写入的条目，请重新预览"),
                status_code=303,
            )
        summary = run_import(sampled, session_factory=SessionLocal, apply=True)
        payload = _random_result_payload(
            summary, requested=len(sampled), sampled=sampled, mode="apply",
        )
        return templates.TemplateResponse(
            request=request,
            name="import_random.html",
            context={
                "count": len(sampled),
                "rtype": rtype,
                "type_options": RANDOM_TYPE_OPTIONS,
                "count_max": RANDOM_COUNT_MAX,
                "error": "",
                "result": payload,
                "detail_labels": sync_tasks.DETAIL_LABELS,
            },
        )

    # 预览：随机偏移翻榜页 + 本地过滤已入库 / nsfw + 随机抽样 + 预映射（不写库）
    try:
        count_val = int(count)
    except (TypeError, ValueError):
        count_val = RANDOM_COUNT_DEFAULT
    if count_val < 1 or count_val > RANDOM_COUNT_MAX:
        return RedirectResponse(
            _url("/manage/import-random", error=f"抓取条数需在 1-{RANDOM_COUNT_MAX} 之间"),
            status_code=303,
        )
    subject_types = _random_subject_types(rtype)
    res = random_import(
        session_factory=SessionLocal,
        count=count_val,
        subject_types=subject_types,
        apply=False,
    )
    if res.pages == 0:
        return RedirectResponse(
            _url(
                "/manage/import-random",
                error="无法访问 Bangumi 排行榜，请检查代理 / 网络后重试",
            ),
            status_code=303,
        )
    if not res.sampled:
        return RedirectResponse(
            _url(
                "/manage/import-random",
                error="候选页里已没有未入库的新作品，可换个类型再试或稍后再来",
            ),
            status_code=303,
        )
    payload = _random_result_payload(
        res.summary, requested=res.requested, sampled=res.sampled, mode="preview",
        pages=res.pages, candidates=res.candidates, duplicates=res.duplicates,
        nsfw_skipped=res.nsfw_skipped, rank_total=res.rank_total,
    )
    return templates.TemplateResponse(
        request=request,
        name="import_random.html",
        context={
            "count": count_val,
            "rtype": rtype,
            "type_options": RANDOM_TYPE_OPTIONS,
            "count_max": RANDOM_COUNT_MAX,
            "error": "",
            "result": payload,
            "ids_str": ",".join(str(s) for s in res.sampled),
            "detail_labels": sync_tasks.DETAIL_LABELS,
        },
    )


# ---------- 作品审核 ----------


@router.get("/manage/submissions", response_class=HTMLResponse)
def submissions_page(
    request: Request,
    db: DbSession,
    status: str = Query(default="pending"),
    page: int = Query(default=1, ge=1),
    decided: str = Query(default=""),
    reason: str = Query(default=""),
):
    """作品审核：按状态筛选的申请列表，支持直接通过 / 驳回。"""
    status_filter = status if status in crud.SUBMISSION_STATUSES else None
    total, submissions = crud.list_content_submissions(
        db, status=status_filter, page=page, page_size=SUBMISSION_PAGE_SIZE
    )
    target_ids = [
        submission.content_id
        for submission in submissions
        if submission.kind == crud.SUBMISSION_KIND_UPDATE
        and submission.content_id is not None
    ]
    target_contents = {
        content.id: content for content in crud.get_contents_by_ids(db, target_ids)
    }
    # 修改申请的新旧对比，以及提交人账号名：避免审核者自己打开作品页逐字比对。
    diffs = {
        submission.id: crud.submission_diff(
            submission,
            target_contents.get(submission.content_id),
            type_labels=CONTENT_TYPE_LABELS,
        )
        for submission in submissions
        if submission.kind == crud.SUBMISSION_KIND_UPDATE
    }
    counts = {
        "pending": crud.count_content_submissions(
            db, status=crud.SUBMISSION_STATUS_PENDING
        ),
        "approved": crud.count_content_submissions(
            db, status=crud.SUBMISSION_STATUS_APPROVED
        ),
        "rejected": crud.count_content_submissions(
            db, status=crud.SUBMISSION_STATUS_REJECTED
        ),
        "all": crud.count_content_submissions(db),
    }
    return templates.TemplateResponse(
        request=request,
        name="admin_submissions.html",
        context={
            "submissions": submissions,
            "total": total,
            "page": page,
            "total_pages": _pages(total, SUBMISSION_PAGE_SIZE),
            "status_filter": status_filter or "",
            "counts": counts,
            "contents": target_contents,
            "diffs": diffs,
            "usernames": crud.get_usernames_by_ids(
                db, [submission.user_id for submission in submissions]
            ),
            "decided": decided,
            "reason": reason,
            "status_labels": crud.SUBMISSION_STATUS_LABELS,
            "kind_labels": crud.SUBMISSION_KIND_LABELS,
            "type_labels": CONTENT_TYPE_LABELS,
        },
    )


@router.post("/manage/submissions/{submission_id}/approve")
def approve_submission(request: Request, db: DbSession, submission_id: int):
    """通过作品申请：写入 / 更新 contents；重复处理返回错误提示。"""
    reviewer = getattr(request.state, "current_user", None)
    _, error = crud.decide_content_submission(
        db,
        submission_id,
        approve=True,
        reviewer_id=reviewer.id if reviewer else 0,
    )
    return RedirectResponse(
        _url(
            "/manage/submissions",
            status="approved",
            decided="ok" if error is None else "error",
            reason=error,
        ),
        status_code=303,
    )


@router.post("/manage/submissions/{submission_id}/reject")
async def reject_submission(request: Request, db: DbSession, submission_id: int):
    """驳回作品申请：可附驳回说明，内容库不变。"""
    form = await request.form()
    note = str(form.get("note", "")).strip()
    reviewer = getattr(request.state, "current_user", None)
    _, error = crud.decide_content_submission(
        db,
        submission_id,
        approve=False,
        reviewer_id=reviewer.id if reviewer else 0,
        note=note,
    )
    return RedirectResponse(
        _url(
            "/manage/submissions",
            status="rejected",
            decided="ok" if error is None else "error",
            reason=error,
        ),
        status_code=303,
    )


# ---------- 同步历史（内容管理：新番档期的同步记录） ----------


@router.get("/manage/sync/runs", response_class=HTMLResponse)
def schedule_runs_page(request: Request, db: DbSession):
    """同步历史页（内容管理）：每次同步（含 dry-run 预览）的运行结果。"""
    total, runs = crud.list_sync_runs(db, limit=100)
    return templates.TemplateResponse(
        request=request,
        name="schedule_runs.html",
        context={"runs": runs, "total": total},
    )
