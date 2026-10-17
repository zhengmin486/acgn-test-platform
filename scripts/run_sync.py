"""定时同步入口：给 cron / Windows 任务计划程序调用的 CLI（不依赖 Web 进程）。

与页面「数据管理 → 数据同步」的关系：
- 本脚本只是 app.sync_tasks.run_task 的命令行包装，同步逻辑、防重锁、
  运行记录（sync_task_runs）、幂等性全部复用同一套实现，不复制、不改写；
- 唯一的差别是 trigger：页面点击写 manual，本脚本写 scheduled，
  历史列表里能看出这条记录是定时任务跑的；
- 单次失败不中断其余数据源，最终退出码非 0，方便 cron 邮件 / 任务计划
  程序告警。

用法示例：
    # 每天跑一次：新番档期（当前档期）+ 官方资讯（推荐 cron 默认行为）
    python scripts/run_sync.py
    # 只同步新番档期（当前档期）
    python scripts/run_sync.py --task yuc_schedule
    # 指定档期
    python scripts/run_sync.py --task yuc_schedule --season 202610
    # 同步指定 Bangumi 条目（必须显式给 ID，没有"默认导哪些"的猜测）
    python scripts/run_sync.py --task bangumi --scope "265 975"
    # 同步官方资讯（可指定栏目与条数）
    python scripts/run_sync.py --task articles --scope "TVアニメ" --limit 10
    # 只预览不写库（仍会写一条 mode=preview 的运行记录，便于审计）
    python scripts/run_sync.py --preview

定时配置见 README「定时更新数据」一节（cron / schtasks 示例）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import sync_tasks  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402

# 默认同步的数据源：新番档期（当前档期）+ 官方资讯。
# Bangumi 条目没有「默认导哪些」的合理猜测，必须显式 --task bangumi --scope "ID..."。
DEFAULT_TASK_KEYS = (sync_tasks.TASK_YUC, sync_tasks.TASK_ARTICLES)
TASK_ALL = "all"

# 退出码：0 全部成功；1 有数据源失败 / 报错；2 用法错误（参数不合法等）。
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_USAGE = 2


def current_season_code(now: datetime | None = None) -> str:
    """当前档期码（YYYYMM）：定时任务不传 --season 时用它。"""
    return (now or datetime.now()).strftime("%Y%m")


def resolve_tasks(task_arg: str | None) -> list[str]:
    """把 --task 参数展开为数据源 key 列表；用法错误抛 ValueError。"""
    if not task_arg or task_arg == TASK_ALL:
        return list(DEFAULT_TASK_KEYS)
    if task_arg == sync_tasks.TASK_BANGUMI:
        return [sync_tasks.TASK_BANGUMI]
    if task_arg == sync_tasks.TASK_YUC:
        return [sync_tasks.TASK_YUC]
    if task_arg == sync_tasks.TASK_ARTICLES:
        return [sync_tasks.TASK_ARTICLES]
    raise ValueError(
        f"未知的数据源：{task_arg}（可选：{TASK_ALL} / {sync_tasks.TASK_BANGUMI} / "
        f"{sync_tasks.TASK_YUC} / {sync_tasks.TASK_ARTICLES}）"
    )


def build_scope(task_key: str, args) -> str:
    """按数据源拼出 scope_text（yuc 缺省自动算当前档期）。"""
    if task_key == sync_tasks.TASK_YUC:
        return args.season or current_season_code()
    if task_key == sync_tasks.TASK_BANGUMI:
        if not (args.scope or "").strip():
            raise ValueError(
                "同步 Bangumi 条目必须显式给出 ID，例如：--scope \"265 975\""
            )
        return args.scope
    return (args.scope or "").strip()  # articles：栏目名，留空=全部


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "定时同步入口（cron / 任务计划程序用）：默认同步「新番档期 + 官方资讯」，"
            "全部逻辑复用 app.sync_tasks.run_task。"
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        metavar="KEY",
        help=(
            f"数据源：{TASK_ALL}（默认，等价于 {sync_tasks.TASK_YUC}+{sync_tasks.TASK_ARTICLES}）、"
            f"{sync_tasks.TASK_BANGUMI}（需配 --scope ID）、{sync_tasks.TASK_YUC}、"
            f"{sync_tasks.TASK_ARTICLES}"
        ),
    )
    parser.add_argument(
        "--season",
        default=None,
        metavar="YYYYMM",
        help="新番档期码；仅 --task yuc_schedule 有效，缺省取当前档期",
    )
    parser.add_argument(
        "--scope",
        default=None,
        metavar="TEXT",
        help="同步范围：Bangumi 条目 ID（空格/逗号分隔）或资讯栏目名；留空=该源默认范围",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="资讯单次条数上限；仅 --task articles 有效，缺省取源站默认",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="只预览不写业务数据（仍写一条 mode=preview 的运行记录）",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None, *, session_factory=None) -> int:
    """CLI 入口：依次同步各数据源；有失败返回非 0 退出码。"""
    args = build_parser().parse_args(argv)

    try:
        task_keys = resolve_tasks(args.task)
    except ValueError as exc:
        print(f"用法错误：{exc}")
        return EXIT_USAGE

    if session_factory is None:
        engine = build_engine(args.database_url)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    apply_mode = not args.preview
    mode_label = "正式写入" if apply_mode else "预览（不写业务数据）"

    print(f"数据库：{args.database_url}")
    print(f"模式：{mode_label}")
    print(f"数据源：{'、'.join(task_keys)}")

    failures = 0
    for task_key in task_keys:
        try:
            scope_text = build_scope(task_key, args)
        except ValueError as exc:
            print(f"[{task_key}] 用法错误：{exc}")
            return EXIT_USAGE

        label = sync_tasks.task_labels().get(task_key, task_key)
        print(f"----- {label}（范围：{scope_text or '默认'}）-----")
        try:
            result = sync_tasks.run_task(
                task_key,
                scope_text,
                apply=apply_mode,
                scope_limit=str(args.limit) if args.limit else None,
                trigger=sync_tasks.TRIGGER_SCHEDULED,
                session_factory=session_factory,
            )
        except (sync_tasks.ScopeError, sync_tasks.TaskBusyError) as exc:
            # 范围不合法 / 正在执行：这条数据源跳过，继续下一个，最后以退出码告警
            print(f"[{label}] 未执行：{exc}")
            failures += 1
            continue
        except Exception as exc:  # 网络异常等：run_task 已把记录标记为失败，这里只回显
            print(f"[{label}] 执行出错：{exc}")
            failures += 1
            continue

        counts = (
            f"新增 {result.added}、更新 {result.updated}、无变化 {result.unchanged}、"
            f"重复 {result.duplicates}、跳过 {result.skipped}、失败 {result.failed}"
        )
        print(f"[{label}] {sync_tasks.STATUS_LABELS[result.status]}：{counts}")
        if result.message:
            print(f"  {result.message}")
        if result.run_id:
            print(f"  运行记录：sync_task_runs.id={result.run_id}（trigger=scheduled）")
        if result.status != sync_tasks.STATUS_OK:
            failures += 1

    if failures:
        print(f"完成：{failures} 个数据源未完全成功。")
        return EXIT_TASK_FAILED
    print("完成：全部数据源同步成功。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
