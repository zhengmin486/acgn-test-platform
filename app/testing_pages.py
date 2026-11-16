"""测试平台页面路由：测试用例管理、执行历史与批次结果（数据来源：数据库访问层）。"""

import json
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import analysis, case_discovery, crud, runner
from app.auth import get_current_user
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


def _filter_cases(cases: list, q: str, method: str, enabled: str) -> list:
    """用例列表筛选：关键词（名称/路径）+ 方法 + 启用状态。

    纯内存过滤（列表规模小、无分页），默认空参时返回全量，行为与原来一致；
    URL 带参即可分享/刷新，支撑「识别优于回忆」。
    """
    keyword = (q or "").strip()
    method_filter = (method or "").strip().upper()
    result = list(cases)
    if keyword:
        result = [c for c in result
                  if keyword.lower() in (c.name or "").lower()
                  or keyword.lower() in (c.path or "").lower()]
    if method_filter:
        result = [c for c in result if (c.method or "").upper() == method_filter]
    if enabled == "1":
        result = [c for c in result if c.enabled]
    elif enabled == "0":
        result = [c for c in result if not c.enabled]
    return result


def _case_filter_view(q: str, method: str, enabled: str) -> dict:
    """筛选项回显（模板 select/input 的 selected/value 用）。"""
    return {"q": q or "", "method": (method or "").strip().upper(), "enabled": enabled or ""}


@router.get("/testcases", response_class=HTMLResponse)
def testcases_page(request: Request, db: DbSession,
                   q: str = Query(default=""), method: str = Query(default=""),
                   enabled: str = Query(default=""),
                   added: int = Query(default=0), updated: int = Query(default=0),
                   disabled: int = Query(default=0), generated: str = Query(default=""),
                   error: str = Query(default="")):
    """用例管理首页：用例列表 + 新建表单；支持关键词/方法/状态筛选（GET，可分享）。

    added / updated / disabled / generated 来自「从接口清单生成」后的 PRG 回显。
    """
    all_cases = crud.list_cases(db)
    cases = _filter_cases(all_cases, q, method, enabled)
    return templates.TemplateResponse(
        request=request,
        name="testcases.html",
        context={"cases": cases, "all_cases": all_cases, "error": error, "form": {},
                 "filter": _case_filter_view(q, method, enabled),
                 "generated": {"added": added, "updated": updated,
                               "disabled": disabled, "error": generated}},
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
                     "all_cases": crud.list_cases(db), "error": _validation_message(exc),
                     "form": form, "filter": _case_filter_view("", "", "")},
            status_code=400,
        )
    except ValueError:
        return templates.TemplateResponse(
            request=request, name="testcases.html",
            context={"cases": crud.list_cases(db),
                     "all_cases": crud.list_cases(db),
                     "error": "预期状态码必须是整数（100-599）", "form": form,
                     "filter": _case_filter_view("", "", "")},
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
    judgment: dict = {}
    run = runner.run_case(db, case, judgment=judgment)
    if run is None:
        reason = judgment.get("reason", "identity_unavailable")
        detail = judgment.get("detail") or reason
        return RedirectResponse(
            "/testcases?error=" + quote(f"用例未执行：{detail}"), status_code=303)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, db: DbSession, case_id: int | None = Query(default=None),
              result: str = Query(default="")):
    """执行历史页：全部记录、按用例过滤或按结果（通过/失败）过滤。"""
    runs_all = crud.list_runs(db, case_id)
    runs = runs_all
    if result == "passed":
        runs = [r for r in runs_all if r.passed]
    elif result == "failed":
        runs = [r for r in runs_all if not r.passed]
    title = f"用例 #{case_id} 的执行历史" if case_id is not None else "执行历史"
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"runs": runs, "runs_all": runs_all, "result_filter": result or "",
                 "title": title, "case_id": case_id},
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


# ---------- 从接口清单自动生成用例 ----------


def _query(**params) -> str:
    """拼装带查询参数的站内地址；空值自动忽略（PRG 回显用）。"""
    clean = {key: value for key, value in params.items() if value not in (None, "", 0)}
    return f"/testcases?{urlencode(clean)}" if clean else "/testcases"


@router.get("/testcases/generate", response_class=HTMLResponse)
def generate_cases_page(request: Request, db: DbSession, notice: str = Query(default="")):
    """生成预览：常规候选 + 异常候选 + 待确认列表，都要人工确认后才生效。"""
    plan = case_discovery.build_plan(db)
    anomaly = case_discovery.build_anomaly_plan(db)
    pending = [c for c in crud.list_cases(db) if c.case_status == "pending_review"]
    return templates.TemplateResponse(
        request=request,
        name="case_generate.html",
        context={"plan": plan, "error": plan.get("error", ""),
                 "anomaly": anomaly, "pending": pending, "notice": notice},
    )


@router.post("/testcases/generate/apply")
async def apply_generated_cases(request: Request, db: DbSession):
    """按勾选结果落库；新增的进待确认，确认后才参与批次（方案 6.3）。"""
    form = await request.form()
    summary = case_discovery.apply_plan(db, form.getlist("select"))
    return RedirectResponse(
        _query(added=summary.get("added"), updated=summary.get("updated"),
               disabled=summary.get("disabled"), generated=summary.get("error", "")),
        status_code=303,
    )


@router.post("/testcases/generate/anomaly")
async def apply_anomaly_cases(request: Request, db: DbSession):
    """把勾选的异常用例落库为待确认（越权、带参、边界各一组）。"""
    form = await request.form()
    summary = case_discovery.apply_anomaly_plan(db, form.getlist("select"))
    notice = quote(f"已加入 {summary.get('added', 0)} 条异常用例，等待确认")
    return RedirectResponse(f"/testcases/generate?notice={notice}", status_code=303)


@router.post("/testcases/confirm")
async def confirm_pending_cases(request: Request, db: DbSession):
    """把选中的待确认用例转正：active + 启用，并记录确认人与时间。

    确认人**只从登录会话取**：请求体里即便带了 confirmed_by 也不读，
    否则留痕本身可以被伪造，答辩时那句「某管理员确认过」就不成立。
    """
    form = await request.form()
    ids = [int(v) for v in form.getlist("case_id") if str(v).isdigit()]
    user = get_current_user(request, db)
    result = case_discovery.confirm_cases(
        db, ids, operator_id=user.id if user else None)
    notice = quote(f"已确认 {result['confirmed']} 条")
    return RedirectResponse(f"/testcases/generate?notice={notice}", status_code=303)


@router.post("/testcases/reject")
async def reject_pending_cases(request: Request, db: DbSession):
    """把选中的待确认用例驳回：rejected + 停用，记录保留可追溯。"""
    form = await request.form()
    ids = [int(v) for v in form.getlist("case_id") if str(v).isdigit()]
    result = case_discovery.reject_cases(db, ids)
    notice = quote(f"已驳回 {result['rejected']} 条")
    return RedirectResponse(f"/testcases/generate?notice={notice}", status_code=303)


@router.post("/testcases/execute-all")
def execute_all_page(request: Request, db: DbSession):
    """执行全部启用用例为一个批次，完成后重定向到批次结果页。"""
    batch = runner.run_batch(db)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/batches", response_class=HTMLResponse)
def batches_page(request: Request, db: DbSession):
    """批次历史页：批次列表 + 趋势（通过率 / 执行量 / 集合变化，仅非 legacy）。"""
    return templates.TemplateResponse(
        request=request,
        name="batches.html",
        context={"batches": crud.list_batches(db),
                 "trend": analysis.trend(db)},
    )


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request, db: DbSession,
                  batch_id: int | None = Query(default=None)):
    """结果分析页：批次/单条归因分开 + 路由分组矩阵 + 最慢用例耗时观察。

    归因分母严格等于各自范围的失败数（方案 7.1）：通过、跳转、未执行都不进来；
    批次与单条执行分开展示、分别统计（v0.6）。可按批次筛选；不传时看全部
    批次与单条执行。legacy 按 `passed=False` 回退取数并标注，不伪造 case_set。
    最慢用例只取有效 duration_ms，是耗时观察不是性能测试，查询只读（9.5）。
    """
    batches = crud.list_batches(db)
    selected = next((b for b in batches if b.id == batch_id), None) if batch_id else None
    distribution = analysis.attribution(db, batch_id)
    single_distribution = analysis.single_attribution(db) if batch_id is None else None
    matrix = analysis.route_group_matrix(db)
    slowest = [
        {"run": run,
         "group": analysis.route_group(run.path),
         "batch_label": (f"#{run.batch_id}" if run.batch_id is not None
                         else "单条执行")}
        for run in analysis.slowest_runs(db, batch_id)
    ]
    legacy_ids = {b.id for b in batches if analysis.is_legacy(b)}
    # 每条失败记录附上归一类与归一后的原因，原文不动、展示只读。
    failures = [
        {"run": run, "defect": analysis.classify_failure(run.error),
         "normalized": analysis.normalize_error(run.error),
         "group": analysis.route_group(run.path)}
        for run in distribution["runs"]
    ]
    single_failures = [
        {"run": run, "defect": analysis.classify_failure(run.error),
         "normalized": analysis.normalize_error(run.error),
         "group": analysis.route_group(run.path)}
        for run in (single_distribution["runs"] if single_distribution else [])
    ]
    return templates.TemplateResponse(
        request=request,
        name="analysis.html",
        context={"batches": batches, "selected_batch_id": batch_id,
                 "selected": selected, "distribution": distribution,
                 "defect_types": analysis.DEFECT_TYPES, "failures": failures,
                 "single_distribution": single_distribution,
                 "single_failures": single_failures,
                 "matrix": matrix, "legacy_ids": legacy_ids, "slowest": slowest},
    )


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail_page(request: Request, batch_id: int, db: DbSession):
    """批次结果页：统计 + 回归对比 + 每条执行记录；批次不存在返回 404。

    二期第 1 步：case_set 为 NULL 的旧批次标 legacy，按旧口径展示，
    不套用新分母、不显示新计数（方案 5.6）。
    """
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        return render_missing(request, "执行批次", batch_id)
    runs = crud.list_runs_for_batch(db, batch_id)
    regression = runner.compare_batches(db, batch)
    flaky_count = sum(1 for item in regression.get("items", [])
                      if item.get("change") == "flaky")
    mixed_count = sum(1 for item in regression.get("items", [])
                      if item.get("change") == "mixed")
    # 通过率统一走 runner 的口径（6.2）：新批次按快照算，legacy 回退旧公式。
    pass_rate = runner.batch_pass_rate(batch)

    # 解析判定快照；legacy 批次不伪造，counts 置空让模板走旧口径分支。
    case_set = None
    if batch.case_set:
        try:
            case_set = json.loads(batch.case_set)
        except (ValueError, TypeError):
            case_set = None
    legacy = case_set is None
    counts = (case_set or {}).get("counts", {})
    not_run = [c for c in (case_set or {}).get("cases", [])
               if c.get("state") == "not_run"] if not legacy else []
    conclusion = None
    if batch.total == 0:
        conclusion = "本批次执行 0 条用例，未验证任何接口。"

    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context={"batch": batch, "runs": runs, "regression": regression,
                 "flaky_count": flaky_count, "mixed_count": mixed_count,
                 "pass_rate": pass_rate, "legacy": legacy, "counts": counts,
                 "not_run": not_run, "conclusion": conclusion},
    )
