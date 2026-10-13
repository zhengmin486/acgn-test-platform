"""Jinja2 页面路由：内容展示 + 测试用例管理（数据来源：数据库访问层）。"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import crud, runner
from app.database import get_db
from app.schemas import TestCaseCreate

# 复用的依赖注解：模块级定义一次，各页面签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["pages"])


def _render_missing(request: Request, subject: str, subject_id):
    """统一 404 页面；subject 区分“内容/测试用例”。"""
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"subject": subject, "content_id": subject_id},
        status_code=404,
    )


# ---------- ACGN 内容展示（契约不变） ----------


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbSession):
    """首页：展示数据库中的全部内容列表。"""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"contents": crud.list_contents(db)},
    )

@router.get("/contents/{content_id}", response_class=HTMLResponse)
def content_detail(request: Request, content_id: int, db: DbSession):
    """详情页；内容不存在时渲染 404 页面并返回 404 状态码。"""
    item = crud.get_content(db, content_id)
    if item is None:
        return _render_missing(request, "内容", content_id)
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={"content": item},
    )
@router.get("/articles", response_class=HTMLResponse)
def articles_page(request: Request, db: DbSession):
    """资讯列表页面。"""
    return templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={"articles": crud.list_articles(db)},
    )


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail_page(
    request: Request,
    article_id: int,
    db: DbSession,
):
    """资讯详情页面。"""
    article = crud.get_article(db, article_id)

    if article is None:
        return _render_missing(request, "资讯", article_id)

    return templates.TemplateResponse(
        request=request,
        name="article_detail.html",
        context={"article": article},
    )

# ---------- 新番档期展示（只读：数据来自同步脚本，不在请求时抓取外部网站） ----------

WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
SCHEDULE_PAGE_SIZE = 20


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request,
    db: DbSession,
    season: str | None = Query(default=None, max_length=6),
    q: str = Query(default=""),
    weekday: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
):
    """新番档期页：档期切换、关键词/星期筛选与分页浏览。

    未指定档期时默认展示最新已同步档期；数据库无任何档期数据时显示引导提示。
    """
    seasons = crud.list_schedule_seasons(db)
    current_season = season or (seasons[0]["season_code"] if seasons else None)
    if weekday is not None and weekday not in WEEKDAY_NAMES:
        weekday = None
    keyword = q.strip() or None

    total = 0
    works: list = []
    total_pages = 1
    season_url = None
    if current_season:
        total, works = crud.list_schedules(
            db,
            season=current_season,
            keyword=keyword,
            weekday=weekday,
            page=page,
            page_size=SCHEDULE_PAGE_SIZE,
        )
        total_pages = max(1, (total + SCHEDULE_PAGE_SIZE - 1) // SCHEDULE_PAGE_SIZE)
        season_url = works[0].source_url if works else None

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "seasons": seasons,
            "current_season": current_season,
            "season_url": season_url,
            "weekday_names": WEEKDAY_NAMES,
            "weekday": weekday,
            "q": q,
            "page": page,
            "page_size": SCHEDULE_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "works": works,
        },
    )


@router.get("/schedule/runs", response_class=HTMLResponse)
def schedule_runs_page(request: Request, db: DbSession):
    """同步历史页：每次同步（含 dry-run 预览）的运行结果。"""
    total, runs = crud.list_sync_runs(db, limit=100)
    return templates.TemplateResponse(
        request=request,
        name="schedule_runs.html",
        context={"runs": runs, "total": total},
    )

# ---------- 测试用例管理页面（本阶段仅管理定义，不执行请求） ----------


def _parse_case_form(name: str, method: str, path: str,
                     expected_status: str, enabled_checked: bool) -> TestCaseCreate:
    """把表单原始输入转成并校验为用例模型；与 API 共用 TestCaseCreate 规则。"""
    return TestCaseCreate(
        name=name,
        method=method,
        path=path,
        expected_status=int(expected_status.strip()),
        enabled=enabled_checked,
    )


def _validation_message(exc: ValidationError) -> str:
    return "；".join(
        "{}：{}".format(".".join(str(loc) for loc in err["loc"]), err["msg"])
        for err in exc.errors()
    )


def _form_values(name: str, method: str, path: str,
                 expected_status: str, enabled: str) -> dict:
    return {"name": name, "method": method, "path": path,
            "expected_status": expected_status, "enabled": enabled}


@router.get("/testcases", response_class=HTMLResponse)
def testcases_page(request: Request, db: DbSession):
    """用例管理首页：用例列表 + 新建表单。"""
    return templates.TemplateResponse(
        request=request,
        name="testcases.html",
        context={"cases": crud.list_cases(db), "error": "", "form": {}},
    )


@router.post("/testcases")
def create_case_page(request: Request, db: DbSession,
                     name: str = Form(""), method: str = Form(""),
                     path: str = Form(""), expected_status: str = Form(""),
                     enabled: str = Form("")):
    """表单新建用例；校验失败返回 400 并回显错误与已填内容。"""
    form = _form_values(name, method, path, expected_status, enabled)
    try:
        data = _parse_case_form(name, method, path, expected_status, enabled == "on")
    except ValidationError as exc:
        return templates.TemplateResponse(
            request=request, name="testcases.html",
            context={"cases": crud.list_cases(db),
                     "error": _validation_message(exc), "form": form},
            status_code=400,
        )
    except ValueError:
        return templates.TemplateResponse(
            request=request, name="testcases.html",
            context={"cases": crud.list_cases(db),
                     "error": "预期状态码必须是整数（100-599）", "form": form},
            status_code=400,
        )
    crud.create_case(db, data)
    return RedirectResponse("/testcases", status_code=303)


@router.get("/testcases/{case_id}/edit", response_class=HTMLResponse)
def edit_case_page(request: Request, case_id: int, db: DbSession):
    """编辑页：预填当前用例；不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        return _render_missing(request, "测试用例", case_id)
    return templates.TemplateResponse(
        request=request,
        name="case_edit.html",
        context={"case": case, "error": ""},
    )


@router.post("/testcases/{case_id}/edit")
def update_case_page(request: Request, case_id: int, db: DbSession,
                     name: str = Form(""), method: str = Form(""),
                     path: str = Form(""), expected_status: str = Form(""),
                     enabled: str = Form("")):
    """表单编辑用例；校验失败返回 400 回显，用例不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        return _render_missing(request, "测试用例", case_id)
    submitted = {"id": case.id, **_form_values(name, method, path, expected_status, enabled)}
    try:
        data = _parse_case_form(name, method, path, expected_status, enabled == "on")
    except ValidationError as exc:
        return templates.TemplateResponse(
            request=request, name="case_edit.html",
            context={"case": submitted, "error": _validation_message(exc)},
            status_code=400,
        )
    except ValueError:
        return templates.TemplateResponse(
            request=request, name="case_edit.html",
            context={"case": submitted, "error": "预期状态码必须是整数（100-599）"},
            status_code=400,
        )
    crud.update_case(db, case, data)
    return RedirectResponse("/testcases", status_code=303)


@router.post("/testcases/{case_id}/delete")
def delete_case_page(request: Request, case_id: int, db: DbSession):
    """删除用例；不存在返回 404，成功后重定向回列表页。"""
    case = crud.get_case(db, case_id)
    if case is None:
        return _render_missing(request, "测试用例", case_id)
    crud.delete_case(db, case)
    return RedirectResponse("/testcases", status_code=303)


@router.post("/testcases/{case_id}/execute")
def execute_case_page(request: Request, case_id: int, db: DbSession):
    """执行单条用例，成功后重定向到本次执行详情页；用例不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        return _render_missing(request, "测试用例", case_id)
    run = runner.run_case(db, case)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, db: DbSession, case_id: int | None = Query(default=None)):
    """执行历史页：全部记录或按用例过滤（用例删除后仍可查看历史）。"""
    runs = crud.list_runs(db, case_id)
    title = f"用例 #{case_id} 的执行历史" if case_id is not None else "执行历史"
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"runs": runs, "title": title, "case_id": case_id},
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(request: Request, run_id: int, db: DbSession):
    """单次执行详情页；记录不存在返回 404。"""
    run = crud.get_run(db, run_id)
    if run is None:
        return _render_missing(request, "执行记录", run_id)
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={"run": run},
    )


@router.post("/testcases/execute-all")
def execute_all_page(request: Request, db: DbSession):
    """执行全部启用用例为一个批次，完成后重定向到批次结果页。"""
    batch = runner.run_batch(db)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/batches", response_class=HTMLResponse)
def batches_page(request: Request, db: DbSession):
    """批次历史页。"""
    return templates.TemplateResponse(
        request=request,
        name="batches.html",
        context={"batches": crud.list_batches(db)},
    )


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail_page(request: Request, batch_id: int, db: DbSession):
    """批次结果页：统计 + 回归对比 + 每条执行记录；批次不存在返回 404。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        return _render_missing(request, "执行批次", batch_id)
    runs = crud.list_runs_for_batch(db, batch_id)
    regression = runner.compare_batches(db, batch)
    pass_rate = round(batch.passed / batch.total * 100, 1) if batch.total else None
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context={"batch": batch, "runs": runs, "regression": regression,
                 "pass_rate": pass_rate},
    )
