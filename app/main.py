"""FastAPI 应用入口。

第二阶段：JSON API 与页面数据改由 SQLAlchemy 数据层提供（app/crud.py）。
连接配置从环境变量 / .env 读取（app/config.py）；
目标数据库为 MySQL，本机服务就绪前默认 SQLite 用于本地开发与验证。
"""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app import admin_pages, api, auth, business_pages, cases, crud, testing_pages
from app.auth import (
    LoginRequired,
    PermissionDenied,
    load_current_user,
    login_required_handler,
    permission_denied_handler,
    require_admin_api,
    require_admin_page,
)
from app.database import engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="ACGN 内容服务与接口自动化测试平台",
    description="ACGN 内容服务与接口自动化测试平台（数据已接入数据库层，本地验证默认 SQLite）。",
    version="0.2.0",
    # 应用级依赖：每个请求解析一次当前登录用户（含内容页面），供模板与守卫复用。
    dependencies=[Depends(load_current_user)],
)

app.include_router(auth.router)
app.include_router(api.router)
# 测试用例 / 执行记录 / 批次 API：仅管理员可访问（未登录 401、越权 403）。
app.include_router(cases.router, dependencies=[Depends(require_admin_api)])
app.include_router(business_pages.router)
# 测试管理平台页面：仅管理员可访问（未登录跳登录页、越权 403）。
app.include_router(testing_pages.router, dependencies=[Depends(require_admin_page)])
# 数据管理页面（测试管理平台 → 数据管理）：同样仅管理员可访问。
app.include_router(admin_pages.router, dependencies=[Depends(require_admin_page)])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 页面级拒绝的统一处理：未登录 -> 登录页；权限不足 -> 403 页面。
app.add_exception_handler(LoginRequired, login_required_handler)
app.add_exception_handler(PermissionDenied, permission_denied_handler)

# 启动即初始化：建表（如无）并播种开发/测试数据；幂等，表已有数据时不覆盖。
# 放在导入期执行，保证 uvicorn 与 TestClient 都拿到就绪的数据库；生产化后换 Alembic。
crud.init_db(engine)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


