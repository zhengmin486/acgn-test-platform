"""把本地开发库重置成「只有表结构」的最小库。

背景（2026-10-18 定型）：真实数据（词条 / 用户 / 资讯 / 档期）只存在服务器上，
本地不允许再攒一份真实数据副本。本地开发与测试只需要一个能跑起来的空结构库。

本脚本做两件事：
1. 把当前本地库改名归档到 ``.workbuddy/db-archive/``（默认保留 3 份，绝不直接删）；
2. 用 ``crud.init_db`` 重建空库——建全部表 + 幂等补列 + 写入初始账号，
   不写任何演示作品（脚本内部临时把 ``SEED_DEMO_DATA`` 置 False）。

所以重置后本地库里只有 users 的两个初始账号，其余表全空。
需要演示作品时用 ``--with-demo`` 重新播种（仍然只是 app/fake_data.py 的假数据，
不是真实词条）。

用法示例：
    python scripts/reset_local_db.py                # 重置本地库（先归档）
    python scripts/reset_local_db.py --keep         # 只归档+重建，不删旧文件
    python scripts/reset_local_db.py --with-demo    # 顺带写入演示假数据
    python scripts/reset_local_db.py --no-archive   # 直接重建，不留备份
    python scripts/reset_local_db.py --database-url sqlite:///./other.db
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402

ARCHIVE_DIR = PROJECT_ROOT / ".workbuddy" / "db-archive"
ARCHIVE_RETAIN = 3


def _sqlite_file_of(url: str) -> Path | None:
    """从 SQLite URL 里取出文件路径；非 SQLite 或内存库返回 None。"""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if raw in ("", ":memory:") or raw.startswith("file:"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def archive(db_path: Path) -> Path | None:
    """把旧库**复制**归档到 ``.workbuddy/db-archive/``，返回归档路径（不存在则 None）。

    用复制而不是移动：本地开发服务可能正开着这个 SQLite 文件，
    Windows 上 rename 会被文件锁拒绝（WinError 32）。原库随后由
    ``purge_business_data`` 清空，只剩表结构。
    """
    if not db_path.exists():
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ARCHIVE_DIR / f"{db_path.stem}-{stamp}{db_path.suffix}"
    shutil.copy2(str(db_path), str(target))

    # 只保留最近几份，避免归档目录无限增长。
    archives = sorted(ARCHIVE_DIR.glob(f"{db_path.stem}-*{db_path.suffix}"))
    for stale in archives[:-ARCHIVE_RETAIN]:
        stale.unlink(missing_ok=True)
    return target


# 明确保留的表：账号体系、配置、测试平台自身的用例/执行记录。
# 本地重置只清「内容与运营数据」，不动这些。
_KEEP_TABLES: frozenset[str] = frozenset(
    {
        # 账号体系：本地开发要能登录，但不是「真实用户数据」（服务器上才是权威副本）。
        "users",
        # 测试平台自身的用例与执行记录：不属于内容数据，本地开发要看它们。
        "test_cases",
        "test_runs",
        "test_batches",
        # 受控词表：标签是识别词条用的基础词，不是真实内容。
        "tags",
    }
)


def purge_business_data(db_path: Path) -> int:
    """就地清空业务表的数据行，只保留表结构与账号/用例，返回清掉的表数。

    实现上是「白名单之外的表全清」：内容平台的数据表（词条、资讯、档期、
    标签、实体、声优、收藏、标注、画像…）都不该在本地攒真实数据；而
    _KEEP_TABLES 里的账号体系与测试平台用例是本地开发要用的，留下。

    逐表 DELETE 而不删文件：这样本地服务不必重启，登录态与初始账号都还在。
    """
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    cleared = 0
    try:
        existing = set(inspect(engine).get_table_names())
        with engine.begin() as conn:
            for table in sorted(existing):
                if table in _KEEP_TABLES or table.startswith("sqlite_"):
                    continue
                conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - 表名来自库内白名单过滤
                cleared += 1
            # 自增主键归零，空库与新库表现一致（仅当库用了 AUTOINCREMENT 才有这张表）。
            if "sqlite_sequence" in existing:
                conn.execute(text("DELETE FROM sqlite_sequence"))
    finally:
        engine.dispose()
    return cleared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置本地开发库为只有表结构的最小库")
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="要重置的数据库 URL；默认取应用配置 DATABASE_URL",
    )
    parser.add_argument("--with-demo", action="store_true", help="顺带写入演示假数据")
    parser.add_argument("--no-archive", action="store_true", help="不归档旧库，直接重建")
    parser.add_argument("--keep", action="store_true", help="与 --no-archive 相反（默认行为）")
    args = parser.parse_args(argv)

    db_path = _sqlite_file_of(args.database_url)
    if db_path is not None and not args.no_archive:
        saved = archive(db_path)
        if saved is not None:
            print(f"旧库已归档：{saved}")
        else:
            print("旧库不存在，直接新建。")

    # 有既有文件时先清空数据行（保留表结构）；没有文件就由 init_db 全新建库。
    if db_path is not None and db_path.exists():
        cleared = purge_business_data(db_path)
        print(f"已清空 {cleared} 张业务表（账号与用例保留）。")
        return 0

    # 重置库不写演示作品：脚本内临时改开关，不影响运行中的服务配置。
    settings.seed_demo_data = bool(args.with_demo)
    engine = build_engine(args.database_url)
    crud.init_db(engine)

    print(f"已重建：{args.database_url}")
    print(f"  演示假数据：{'写入' if args.with_demo else '未写入'}")
    print("  真实词条/资讯/用户数据只在服务器，本地请从服务器导入或联机验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
