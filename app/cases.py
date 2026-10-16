"""测试用例管理 JSON API（只管理用例定义，不执行请求）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crud, runner
from app.database import get_db
from app.schemas import TestCaseCreate

# 复用的依赖注解：模块级定义一次，各端点签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

router = APIRouter(tags=["testcases"])


@router.get("/api/testcases")
def list_cases(db: DbSession) -> dict:
    """用例列表。"""
    cases = crud.list_cases(db)
    return {"total": len(cases), "items": [c.as_dict() for c in cases]}


@router.post("/api/testcases", status_code=201)
def create_case(data: TestCaseCreate, db: DbSession) -> dict:
    """新建用例；入参由 TestCaseCreate 校验，非法输入自动返回 422。"""
    return crud.create_case(db, data).as_dict()


@router.get("/api/testcases/{case_id}")
def get_case(case_id: int, db: DbSession) -> dict:
    """用例详情；不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"测试用例不存在: id={case_id}")
    return case.as_dict()


@router.put("/api/testcases/{case_id}")
def update_case(case_id: int, data: TestCaseCreate, db: DbSession) -> dict:
    """编辑用例（提交完整字段）；不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"测试用例不存在: id={case_id}")
    return crud.update_case(db, case, data).as_dict()


@router.delete("/api/testcases/{case_id}", status_code=204)
def delete_case(case_id: int, db: DbSession) -> None:
    """删除用例；不存在返回 404，成功返回 204 无响应体。"""
    case = crud.get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"测试用例不存在: id={case_id}")
    crud.delete_case(db, case)


@router.post("/api/testcases/{case_id}/execute", status_code=201)
def execute_case(case_id: int, db: DbSession) -> dict:
    """执行单条用例并返回本次执行记录；用例不存在返回 404。"""
    case = crud.get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"测试用例不存在: id={case_id}")
    return runner.run_case(db, case).as_dict()


@router.get("/api/runs")
def list_runs(db: DbSession, case_id: int | None = Query(default=None),
              batch_id: int | None = Query(default=None)) -> dict:
    """执行记录列表（最新在前）；可用 case_id/batch_id 过滤，历史不随删除丢失。"""
    runs = crud.list_runs(db, case_id, batch_id)
    return {"total": len(runs), "items": [r.as_dict() for r in runs]}


@router.get("/api/runs/{run_id}")
def get_run(run_id: int, db: DbSession) -> dict:
    """执行记录详情；不存在返回 404。"""
    run = crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"执行记录不存在: id={run_id}")
    return run.as_dict()


@router.post("/api/batches", status_code=201)
def run_batch_endpoint(db: DbSession) -> dict:
    """把全部启用用例作为一批执行；完成时返回批次统计（空用例集亦为有效批次）。"""
    return runner.run_batch(db).as_dict()


@router.get("/api/batches")
def list_batches(db: DbSession) -> dict:
    """批次列表（最新在前）。"""
    batches = crud.list_batches(db)
    return {"total": len(batches), "items": [b.as_dict() for b in batches]}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, db: DbSession) -> dict:
    """批次统计详情；不存在返回 404。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"执行批次不存在: id={batch_id}")
    return batch.as_dict()


@router.get("/api/batches/{batch_id}/regression")
def batch_regression(batch_id: int, db: DbSession) -> dict:
    """当前批次与上一有效批次的回归对比；批次不存在返回 404。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"执行批次不存在: id={batch_id}")
    return runner.compare_batches(db, batch)
