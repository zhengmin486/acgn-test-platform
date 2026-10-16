"""测试平台页面路由：测试用例管理、执行历史与批次结果（数据来源：数据库访问层）。"""

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import crud, runner
from app.page_common import DbSession, render_missing, templates
from app.schemas import TestCaseCreate

router = APIRouter(tags=["pages"])


# ---------- 测试用例管理页面（只管理定义，不执行请求） ----------


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
        return render_missing(request, "测试用例", case_id)
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
        return render_missing(request, "测试用例", case_id)
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
        return render_missing(request, "测试用例", case_id)
    crud.delete_case(db, case)
    return RedirectResponse("/testcases", status_code=303)


@router.post("/testcases/{case_id}/execute")
def execute_case_page(request: Request, case_id: int, db: DbSession):
    """执行单条用例，成功后重定向到本次执行详情页；用例不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        return render_missing(request, "测试用例", case_id)
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
        return render_missing(request, "执行记录", run_id)
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
        return render_missing(request, "执行批次", batch_id)
    runs = crud.list_runs_for_batch(db, batch_id)
    regression = runner.compare_batches(db, batch)
    pass_rate = round(batch.passed / batch.total * 100, 1) if batch.total else None
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context={"batch": batch, "runs": runs, "regression": regression,
                 "pass_rate": pass_rate},
    )
