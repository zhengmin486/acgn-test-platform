"""定时同步入口：给 cron / Windows 任务计划程序调用的 CLI（不依赖 Web 进程）。

与页面「数据管理 → 数据同步」的关系：
- 本脚本只是 app.sync_tasks.run_task 的命令行包装，同步逻辑、防重锁、
  运行记录（sync_task_runs）、幂等性全部复用同一套实现，不复制、不改写；
- 唯一的差别是 trigger：页面点击写 manual，本脚本写 scheduled，
  历史列表里能看出这条记录是定时任务跑的；
- 单次失败不中断其余数据源，最终退出码非 0，方便 cron 邮件 / 任务计划
  程序告警。

特殊档期分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）不在 Atom 的
/YYYYMM/ 季度发现路径里，必须按分类码单独同步；cron 每次跑完季度档期后
接力跑这三个（见 special_category_codes 与 main 的接力逻辑），否则三个
分类页永远是空的（2026-10-24 前即因无人触发而全为 0）。

用法示例：
    # 每天跑一次：新番档期（Atom 里的最新一期）+ 三个特殊分类
    # （未上线新番/剧场版ova与特别篇/大陆引进剧场版）+ 多源资讯（按自然日配额）
    python scripts/run_sync.py
    # 只同步新番档期（自动取最新一期）
    python scripts/run_sync.py --task yuc_schedule
    # 指定档期
    python scripts/run_sync.py --task yuc_schedule --season 202610
    # 同步多源资讯（每日 30 条配额，日语源优先 + 题材优先级，跨次累计）
    python scripts/run_sync.py --task news
    # 同步指定 Bangumi 条目（必须显式给 ID，没有"默认导哪些"的猜测）
    python scripts/run_sync.py --task bangumi --scope "265 975"
    # 同步官方资讯（Aniplex 单源，可指定栏目与条数）
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

# 默认同步的数据源：新番档期（自动取最新档期）+ 多源资讯（按自然日配额）。
# Bangumi 条目没有「默认导哪些」的合理猜测，必须显式 --task bangumi --scope "ID..."，
# 也不能进 cron——「作品不自动」是既定口径。
DEFAULT_TASK_KEYS = (sync_tasks.TASK_YUC, sync_tasks.TASK_NEWS)
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
    if task_arg == sync_tasks.TASK_NEWS:
        return [sync_tasks.TASK_NEWS]
    raise ValueError(
        f"未知的数据源：{task_arg}（可选：{TASK_ALL} / {sync_tasks.TASK_BANGUMI} / "
        f"{sync_tasks.TASK_YUC} / {sync_tasks.TASK_ARTICLES} / {sync_tasks.TASK_NEWS}）"
    )


def resolve_latest_season(*, timeout: float = 15.0) -> str:
    """从 Atom 取最新档期码（如 202610）；取不到时退回当前年月。

    不能用「当前年月」当档期：yuc.wiki 只在 1/4/7/10 月出表，
    11、12 月算出来的 202611 并不存在，同步会天天失败。
    """
    from scripts.sync_yuc_schedule import (  # 局部导入：避免脚本启动即建 opener
        ATOM_URL,
        build_opener,
        fetch_url,
        parse_atom,
        resolve_insecure,
    )

    try:
        text = fetch_url(
            ATOM_URL, opener=build_opener(None, resolve_insecure(None)), timeout=timeout
        )
        seasons = parse_atom(text)
        if seasons:
            return seasons[0].code
    except Exception:  # noqa: BLE001 - 取不到就退回当前年月，让同步自己去报错
        pass
    return current_season_code()


def build_scope(task_key: str, args) -> str:
    """按数据源拼出 scope_text（yuc 缺省取 Atom 里的最新档期）。"""
    if task_key == sync_tasks.TASK_YUC:
        return args.season or resolve_latest_season()
    if task_key == sync_tasks.TASK_NEWS:
        return ""  # 配额由配置决定，无需范围
    if task_key == sync_tasks.TASK_BANGUMI:
        if not (args.scope or "").strip():
            raise ValueError(
                "同步 Bangumi 条目必须显式给出 ID，例如：--scope \"265 975\""
            )
        return args.scope
    return (args.scope or "").strip()  # articles：栏目名，留空=全部


def special_category_codes() -> list[str]:
    """特殊档期分类码：未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版。

    它们不是 YYYYMM 档期，不在 Atom 的季度发现路径里（分类码同时充当
    season_code 与 category），必须按分类码走 run_task(TASK_YUC, code)——
    该 scope 由 app.sync_tasks 特殊处理（读 Atom 定位分类页再抓），且不做
    作品库自动补库（档期窗口算不出来）。
    """
    from scripts.sync_yuc_schedule import (  # 局部导入：避免脚本启动即建 opener
        SPECIAL_CATEGORIES,
    )

    return [c["code"] for c in SPECIAL_CATEGORIES]


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
            f"数据源：{TASK_ALL}（默认，等价于 {sync_tasks.TASK_YUC}+{sync_tasks.TASK_NEWS}）、"
            f"{sync_tasks.TASK_BANGUMI}（需配 --scope ID）、{sync_tasks.TASK_YUC}、"
            f"{sync_tasks.TASK_NEWS}（多源每日资讯）、{sync_tasks.TASK_ARTICLES}（Aniplex 单源）"
        ),
    )
    parser.add_argument(
        "--season",
        default=None,
        metavar="YYYYMM",
        help="新番档期码；仅 --task yuc_schedule 有效，缺省取 Atom 里的最新一期",
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


def _run_one(
    task_key: str,
    scope_text: str,
    *,
    apply: bool,
    scope_limit: str | None,
    session_factory,
) -> tuple[sync_tasks.TaskResult | None, int]:
    """执行一个 (数据源, 范围) 组合并打印结果；返回 (结果, 失败计数)。

    抽出来供「季度档期」与「特殊分类接力」共用：执行、打印、异常处理完全
    一致；失败不抛给调用方，只以失败计数汇总，最后统一决定退出码。
    """
    label = sync_tasks.task_labels().get(task_key, task_key)
    print(f"----- {label}（范围：{scope_text or '默认'}）-----")
    try:
        result = sync_tasks.run_task(
            task_key,
            scope_text,
            apply=apply,
            scope_limit=scope_limit,
            trigger=sync_tasks.TRIGGER_SCHEDULED,
            session_factory=session_factory,
        )
    except (sync_tasks.ScopeError, sync_tasks.TaskBusyError) as exc:
        # 范围不合法 / 正在执行：这条跳过，继续下一个，最后以退出码告警
        print(f"[{label}] 未执行：{exc}")
        return None, 1
    except Exception as exc:  # 网络异常等：run_task 已把记录标记为失败，这里只回显
        print(f"[{label}] 执行出错：{exc}")
        return None, 1

    counts = (
        f"新增 {result.added}、更新 {result.updated}、无变化 {result.unchanged}、"
        f"重复 {result.duplicates}、跳过 {result.skipped}、失败 {result.failed}"
    )
    print(f"[{label}] {sync_tasks.STATUS_LABELS[result.status]}：{counts}")
    if result.message:
        print(f"  {result.message}")
    if result.run_id:
        print(f"  运行记录：sync_task_runs.id={result.run_id}（trigger=scheduled）")
    return result, 0 if result.status == sync_tasks.STATUS_OK else 1


# 自动复核高分段评分人数的口径：>9 分的作品一旦缺评分人数，会以「未知」身份
# 被当成可信分数顶到「评分最高」最前面，所以同步收尾必须补查一遍。
HIGH_RATING_REVIEW_MIN = 9.0
HIGH_RATING_REVIEW_LIMIT = 300


def _review_high_ratings() -> int:
    """同步后复核高分段作品的评分人数；返回 1 表示这一步失败。

    直接复用 scripts/backfill_rating_count.py（同一套判定口径与限速），
    不在这里另写一份请求逻辑，避免两处行为漂移。
    """
    # 复核要回源查 Bangumi：官方 api.bgm.tv 在服务器上不可达（Network unreachable），
    # 定时环境如果没配镜像地址，这里兜底用镜像；已配则完全不动。
    # 必须**先** setdefault 再 import —— 镜像地址是在模块导入时读取的。
    import os

    os.environ.setdefault("BANGUMI_API_BASE", "https://bgmapi.anibt.net")
    from scripts.backfill_rating_count import main as backfill_rating_count_main

    print()
    print(
        f"[rating_review] 复核评分 >= {HIGH_RATING_REVIEW_MIN} 的作品"
        f"（最多 {HIGH_RATING_REVIEW_LIMIT} 条）的评分人数…"
    )
    try:
        code = backfill_rating_count_main(
            [
                "--min-rating",
                str(HIGH_RATING_REVIEW_MIN),
                "--limit",
                str(HIGH_RATING_REVIEW_LIMIT),
                "--apply",
            ]
        )
    except Exception as exc:  # 复核失败不影响同步主体结果，但要计入失败数
        print(f"[rating_review] 失败：{exc}")
        return 1
    return 0 if code == 0 else 1


def main(argv=None, *, session_factory=None) -> int:
    """CLI 入口：依次同步各数据源；有失败返回非 0 退出码。"""
    args = build_parser().parse_args(argv)

    try:
        task_keys = resolve_tasks(args.task)
    except ValueError as exc:
        print(f"用法错误：{exc}")
        return EXIT_USAGE

    session_factory_injected = session_factory is not None
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

        _result, failed = _run_one(
            task_key,
            scope_text,
            apply=apply_mode,
            scope_limit=str(args.limit) if args.limit else None,
            session_factory=session_factory,
        )
        failures += failed

        # 新番档期接力同步三个特殊分类（未上线新番 / 剧场版ova与特别篇 /
        # 大陆引进剧场版）：它们不进 Atom 季度发现路径，不接力就永远是空的。
        # 与季度档期各自独立运行——季度失败不挡住分类，某个分类失败也不
        # 影响其余数据源。幂等，每天重复跑只有「无变化」。
        if task_key == sync_tasks.TASK_YUC:
            for code in special_category_codes():
                _cat_result, cat_failed = _run_one(
                    task_key,
                    code,
                    apply=apply_mode,
                    scope_limit=None,
                    session_factory=session_factory,
                )
                failures += cat_failed

    # 同步收尾：自动复核高分段作品的「评分人数」。
    # >9 分的作品会被顶到「评分最高」最前面，必须确认它到底有多少人打分 ——
    # Bangumi 上两三个人就能打出 9.5，不该和万人打出的 9.5 并列
    # （2026-10-28 实测：冒险兄弟 / 米菲 / Незнайка 的 9.5 都只有 2 人打分）。
    # 幂等：只查「还没查过人数」的行，重复跑只有「无变化」。
    # 仅在正式写入时跑；测试注入 session_factory 时跳过，避免单测打网络。
    if apply_mode and session_factory_injected is False:
        failures += _review_high_ratings()

    if failures:
        print(f"完成：{failures} 个数据源未完全成功。")
        return EXIT_TASK_FAILED
    print("完成：全部数据源同步成功。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
