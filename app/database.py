"""数据库连接配置：引擎、会话工厂与 get_db 依赖。

目标数据库为 MySQL（pymysql 驱动）；本机 MySQL 服务就绪前，
DATABASE_URL 默认使用 SQLite 完成模型与 CRUD 验证。
"""

from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """声明式基类；后续由 Alembic 接管迁移。"""


def build_engine(url: str, *, busy_timeout: float | None = None) -> Engine:
    """按 URL 创建引擎；SQLite 需允许跨线程访问（FastAPI 线程池调用）。

    busy_timeout：SQLite 写锁等待秒数。脚本与线上服务共用同一个库文件时，
    并发写会抛 `database is locked`，把等待时间调宽即可（MySQL 无此问题）。

    连接池参数（pool_size / max_overflow / 超时与回收 / pre_ping）走配置
    （app.config 的 DB_POOL_*），只对非 SQLite URL 生效——SQLite 沿用
    默认池行为，本地开发与测试不受影响。
    """
    kwargs: dict = {}
    if url.startswith("sqlite"):
        connect_args: dict = {"check_same_thread": False}
        if busy_timeout is not None:
            connect_args["timeout"] = busy_timeout
        kwargs["connect_args"] = connect_args
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
            pool_pre_ping=settings.db_pool_pre_ping,
        )
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
