"""FastAPI 应用入口。

第二阶段：JSON API 与页面数据改由 SQLAlchemy 数据层提供（app/crud.py）。
连接配置从环境变量 / .env 读取（app/config.py）；
目标数据库为 MySQL，本机服务就绪前默认 SQLite 用于本地开发与验证。
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, cases, crud, pages
from app.database import engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="ACGN 内容服务与接口自动化测试平台",
    description="ACGN 内容服务与接口自动化测试平台（数据已接入数据库层，本地验证默认 SQLite）。",
    version="0.1.0",
)

app.include_router(api.router)
app.include_router(cases.router)
app.include_router(pages.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 启动即初始化：建表（如无）并播种开发/测试数据；幂等，表已有数据时不覆盖。
# 放在导入期执行，保证 uvicorn 与 TestClient 都拿到就绪的数据库；生产化后换 Alembic。
crud.init_db(engine)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
