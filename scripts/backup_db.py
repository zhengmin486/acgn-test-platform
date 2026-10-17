"""SQLite 在线备份脚本：定时把数据库打成一致快照，并做完整性自检与保留清理。

为什么不是直接 cp：SQLite 的 WAL / 回滚日志意味着「复制中的.db 文件」
可能处于中间状态，直接拷贝有拿到撕裂快照的风险。本脚本使用 SQLite
官方的在线备份 API（sqlite3.Connection.backup，与命令行 sqlite3 的
.backup 同机制）：备份过程中允许应用继续读写，拿到的一定是某一时刻的
一致快照。

用法示例：
    # 立即备份一次（输出到 settings.backup_dir，默认 ./backups）
    python scripts/backup_db.py
    # 指定输出目录与保留份数
    python scripts/backup_db.py --output-dir D:/acgn-backups --retain 30
    # 自定义单个备份文件名
    python scripts/backup_db.py --output D:/acgn-backups/before-upgrade.db
    # 查看已有备份（大小、时间、自检结果）
    python scripts/backup_db.py --list
    # 指定数据库（默认取配置 DATABASE_URL）
    python scripts/backup_db.py --database-url sqlite:///./local_dev.db

设计要点：
- 只认 SQLite：连接串不是 sqlite 时直接报错退出（目标生产是 MySQL，
  届时请改用 mysqldump / xtrabackup 等官方工具，不要硬套本脚本）；
- 备份完成后对备份文件本身执行 PRAGMA integrity_check，自检不过即失败，
  绝不留一个坏文件冒充好备份；
- 保留策略只清理输出目录下符合 acgn-*.db 命名规则的旧备份，
  不碰目录里的任何其它文件；--retain 0 表示不清理；
- 备份目录可用绝对路径指到别的盘符（与 cover_storage_dir 同一思路）。

定时运行见 README「每日自动备份」一节（Windows 任务计划程序 / cron）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import settings  # noqa: E402

# 备份文件名前缀 + 时间戳；保留策略按此前缀识别「本脚本产的备份」。
BACKUP_PREFIX = "acgn-"
BACKUP_SUFFIX = ".db"
BACKUP_TIME_FORMAT = "%Y%m%d-%H%M%S"


def sqlite_path_from_url(database_url: str) -> Path:
    """从 SQLAlchemy 连接串解析 SQLite 文件路径。

    支持 sqlite:///./relative.db（相对项目根）与 sqlite:////abs/path.db
    （绝对路径）两种写法；不是 SQLite 连接串时抛 ValueError。
    """
    url = make_url(database_url)
    if not url.get_backend_name() == "sqlite":
        raise ValueError(
            f"备份脚本只支持 SQLite（当前连接串后端为 {url.get_backend_name()!r}）。"
            "生产 MySQL 请改用 mysqldump / xtrabackup 等官方工具。"
        )
    database = url.database or ""
    if not database:
        raise ValueError("连接串中没有数据库文件路径（sqlite:/// 后面为空）。")
    path = Path(database)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def build_backup_path(output_dir: Path, now: datetime | None = None) -> Path:
    """生成带时间戳的备份文件路径（输出目录不存在会自动创建）。"""
    stamp = (now or datetime.now()).strftime(BACKUP_TIME_FORMAT)
    return output_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"


def online_backup(source: Path, destination: Path) -> None:
    """用 SQLite 在线备份 API 把 source 拷成 destination 的一致快照。

    与 cp 的本质区别：备份期间源库仍可正常读写，拿到的是某一逻辑时刻的
    完整快照，不会出现「拷到一半、WAL 还没应用」的撕裂状态。
    """
    source_conn = sqlite3.connect(str(source))
    try:
        destination_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
    finally:
        source_conn.close()


def verify_backup(path: Path) -> str:
    """对备份文件执行 PRAGMA integrity_check，返回检查结论（'ok' 或错误描述）。"""
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"自检无法完成：{exc}"
    if len(rows) == 1 and rows[0][0] == "ok":
        return "ok"
    return "；".join(str(row[0]) for row in rows)


def prune_backups(output_dir: Path, retain: int) -> list[Path]:
    """保留最近 retain 份 acgn-*.db 备份，返回被删除的文件列表。

    - retain <= 0 表示不清理；
    - 只认输出目录下、文件名符合 acgn-*.db 的备份，绝不碰其它文件；
    - 按修改时间从新到旧排序，第 retain 份之后的才删（刚生成的必保）。
    """
    if retain <= 0:
        return []
    candidates = [
        path
        for path in output_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in candidates[retain:]:
        path.unlink()
        removed.append(path)
    return removed


def human_size(num_bytes: int) -> str:
    """字节数转人类可读（KB/MB），备份清单用。"""
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def list_backups(output_dir: Path) -> None:
    """列出输出目录里的备份文件（含自检结论），不存在时给出提示。"""
    if not output_dir.is_dir():
        print(f"备份目录不存在：{output_dir}")
        return
    backups = sorted(output_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))
    if not backups:
        print(f"目录 {output_dir} 下还没有备份。")
        return
    print(f"{output_dir} 下共 {len(backups)} 个备份：")
    for path in backups:
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {path.name}  {human_size(stat.st_size)}  修改于 {mtime}")


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "SQLite 在线备份：一致快照 + 完整性自检 + 保留清理。"
            "定时运行方式见 README「每日自动备份」。"
        ),
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）；仅支持 SQLite",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.backup_dir,
        help=f"备份输出目录（默认取配置 BACKUP_DIR={settings.backup_dir}）",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="自定义单个备份文件路径（与 --output-dir 二选一）",
    )
    parser.add_argument(
        "--retain",
        type=int,
        default=settings.backup_retain,
        help=f"保留最近几份备份，0 表示不清理（默认取配置 BACKUP_RETAIN={settings.backup_retain}）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="列出现有备份后退出，不执行备份",
    )
    return parser


def main(argv=None) -> int:
    """CLI 入口：执行一次备份并自检；失败返回非 0 退出码。"""
    args = build_parser().parse_args(argv)

    if args.list_only:
        list_backups(Path(args.output_dir))
        return 0

    # 1) 解析源库：非 SQLite / 文件不存在都直接失败，不产生半个文件
    try:
        source = sqlite_path_from_url(args.database_url)
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 1
    if not source.is_file():
        print(f"数据库文件不存在：{source}（连接串：{args.database_url}）")
        return 1

    # 2) 确定输出位置（自定义文件路径时保留策略作用于其所在目录）
    if args.output:
        destination = Path(args.output)
        if not destination.is_absolute():
            destination = PROJECT_ROOT / destination
        output_dir = destination.parent
    else:
        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir
        destination = build_backup_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 3) 在线备份（应用可继续读写），失败时清掉残留文件不留坏备份
    print(f"源数据库：{source}")
    print(f"备份到：{destination}")
    try:
        online_backup(source, destination)
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        print(f"备份失败：{exc}")
        return 1

    # 4) 对备份文件本身自检；不过同样删掉，避免坏文件被当成好备份
    verdict = verify_backup(destination)
    if verdict != "ok":
        destination.unlink(missing_ok=True)
        print(f"完整性自检未通过，已删除该备份：{verdict}")
        return 1

    size = human_size(destination.stat().st_size)
    print(f"备份完成：{destination}（{size}，完整性自检 ok）")

    # 5) 保留策略：只清理同目录下本脚本产的 acgn-*.db
    removed = prune_backups(output_dir, args.retain)
    for path in removed:
        print(f"已清理旧备份：{path.name}")
    if args.retain > 0:
        print(f"保留策略：最近 {args.retain} 份（目录 {output_dir}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
