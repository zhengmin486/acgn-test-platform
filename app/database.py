"""数据库连接配置：引擎、会话工厂与 get_db 依赖。

目标数据库为 MySQL（pymysql 驱动）；本机 MySQL 服务就绪前，
DATABASE_URL 默认使用 SQLite 完成模型与 CRUD 验证。
"""

from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """声明式基类；下阶段由 Alembic 接管迁移。"""


def build_engine(url: str) -> Engine:
    """按 URL 创建引擎；SQLite 需允许跨线程访问（FastAPI 线程池调用）。"""
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine: Engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话，结束后必关。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
