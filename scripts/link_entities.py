"""作品关系回填：给库中已有作品补「作者/公司关联 + 所属系列」。

适用场景：
- 关系功能上线前导入的作品没有作者/公司关联（content_entity_links 为空）；
- 老作品的 infobox 解析规则更新后想重算（--force）；
- 只想给某几部作品补关系（--content-id 指定）；
- 补「所属系列」（--series）：走 Bangumi 关联条目端点，仅当配对作品
  也在库内时才按标题公共前缀归系列（拿单边标题硬凑会造出错误分组）。

设计要点（与 scripts/link_articles.py 同一风格）：
- 识别逻辑全部复用 app/entity_links（infobox 白名单 + 替换式写入），本脚本
  只负责「挑作品 -> 拉条目 -> 识别 -> （可选）写库 -> 汇总」，不复制识别算法；
- 网络层复用 scripts/import_bangumi.py（fetch_subject /
  fetch_related_subjects / build_opener），不另写一套 urllib；
- 默认只预览：照样做识别计算并打印结果，但不写库；显式 --apply 才写入；
- 已有关系数据的作品默认跳过（--force 才重算），重复运行成本低；
- 单条作品拉取失败只记 failed，不中断其余作品；
- 退出码：0 正常完成；1 指定了 --content-id 但一部都没匹配到；2 用法错误。

用法示例：
    # 预览：对所有作品做 infobox 识别，不写库
    python scripts/link_entities.py
    # 正式回填（写入 content_entity_links）
    python scripts/link_entities.py --apply
    # 已有关系的也重算
    python scripts/link_entities.py --apply --force
    # 只处理指定作品
    python scripts/link_entities.py --apply --content-id 7 8 9
    # 同时补所属系列（每部作品多一次关联条目请求）
    python scripts/link_entities.py --apply --series
    # 走本机代理（也可用 BANGUMI_PROXY 环境变量）
    python scripts/link_entities.py --series --proxy http://127.0.0.1:9910
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud, entity_links  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    BANGUMI_API_BASE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    build_opener,
    fetch_related_subjects,
    fetch_subject,
    resolve_proxy,
)

# 退出码：0 正常完成；1 指定作品一部都没匹配到；2 用法错误。
EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_USAGE = 2


@dataclass
class LinkOutcome:
    """单部作品的关系识别结果。"""

    content_id: int
    title: str
    status: str  # recognized / pending / skipped / failed
    links: list[tuple[str, str, str]] = field(default_factory=list)
    series: str | None = None
    reason: str = ""

    def describe(self) -> str:
        label = {
            "recognized": "已识别",
            "pending": "待识别(预览)",
            "skipped": "跳过",
            "failed": "失败",
        }.get(self.status, self.status)
        parts = [f"[{label}] #{self.content_id} 《{self.title}》"]
        if self.links:
            rendered = "；".join(f"{role}：{name}" for name, _type, role in self.links)
            parts.append(f"作者/公司：{rendered}")
        elif self.status != "skipped":
            # 跳过的原因已在 reason 里说明（无 bangumi_id / 已有数据 / 未识别到）
            parts.append("作者/公司：未识别到")
        if self.series:
            parts.append(f"系列：{self.series}")
        if self.reason:
            parts.append(f"- {self.reason}")
        return " ".join(parts)


@dataclass
class LinkSummary:
    """一次回填运行的汇总统计。"""

    mode: str  # preview / apply
    requested: int = 0
    recognized: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    linked: int = 0  # 识别出至少一个作者/公司关联的部数
    series_set: int = 0  # 归入系列的部数
    outcomes: list[LinkOutcome] = field(default_factory=list)

    def describe(self) -> str:
        mode_label = "正式回填（--apply）" if self.mode == "apply" else "预览（dry-run）"
        return "\n".join(
            [
                f"模式：{mode_label}",
                "回填汇总：",
                f"  作品总数：{self.requested}",
                f"  实际识别：{self.recognized}（预览待识别 {self.pending}）",
                f"  跳过：{self.skipped}",
                f"  失败：{self.failed}",
                f"  识别出作者/公司：{self.linked} 部",
                f"  归入系列：{self.series_set} 部",
            ]
        )


def list_target_contents(db, content_ids: list[int] | None) -> list[Content]:
    """挑出要处理的作品（按 id 升序）；给了 content_ids 就只取这些。"""
    stmt = select(Content).order_by(Content.id)
    if content_ids:
        stmt = stmt.where(Content.id.in_(content_ids))
    return list(db.scalars(stmt))


def find_content_by_bangumi(db, bangumi_id: int) -> Content | None:
    """按 bangumi_id 找库内作品（系列配对用；找不到说明不在库内）。"""
    return db.scalar(select(Content).where(Content.bangumi_id == bangumi_id))


def series_from_related(db, content: Content, related_payload) -> str | None:
    """从关联条目里找同系列配对，返回系列名（找不到返回 None）。

    两道闸门，缺一不可：
    - 关系类型命中 SERIES_RELATIONS（续集 / 前传 / 外传 / 衍生 / 总集篇…）；
    - 配对条目按 bangumi_id 能在库内找到——拿单边标题硬凑系列，
      会把「同名前作」错归成一部作品的同学，宁可不要。
    系列名取两个标题的公共前缀（entity_links.common_series_name），
    首个有效配对即采用。
    """
    for subject_id, relation, title in entity_links.parse_related_subjects(related_payload):
        if not entity_links.is_series_relation(relation):
            continue
        if subject_id == content.bangumi_id:
            continue
        pair = find_content_by_bangumi(db, subject_id)
        if pair is None:
            continue
        name = entity_links.common_series_name(content.title, pair.title)
        if name:
            return name
    return None


def build_fetchers(
    *,
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    sleep=time.sleep,
):
    """按代理/超时配置构建 (fetch_subject, fetch_related_subjects) 一对函数。

    测试可自行注入这两个参数绕过网络（run_backfill 的 fetch / fetch_related）。
    """
    opener = build_opener(proxy)
    fetch = partial(
        fetch_subject,
        opener=opener,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        base_url=base_url,
        sleep=sleep,
    )
    fetch_related = partial(
        fetch_related_subjects,
        opener=opener,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        base_url=base_url,
        sleep=sleep,
    )
    return fetch, fetch_related


def run_backfill(
    session_factory,
    *,
    apply: bool = False,
    force: bool = False,
    content_ids: list[int] | None = None,
    series: bool = False,
    fetch=None,
    fetch_related=None,
) -> LinkSummary:
    """回填主流程：挑作品 -> 拉条目 -> 识别 -> （apply 时）写库 -> 汇总。

    force：已有关系数据的作品也重新识别（替换式写入，不会堆叠关联行）。
    content_ids：只处理指定作品（None = 全部）。
    series：同时走关联条目端点补所属系列（每部作品多一次请求）。
    fetch / fetch_related：网络注入点（测试用）；None 时由调用方构建。
    """
    summary = LinkSummary(mode="apply" if apply else "preview")
    db = session_factory()
    try:
        contents = list_target_contents(db, content_ids)

        for content in contents:
            summary.requested += 1
            if not content.bangumi_id:
                summary.skipped += 1
                summary.outcomes.append(
                    LinkOutcome(
                        content.id,
                        content.title,
                        "skipped",
                        reason="没有 bangumi_id，无法从源站取信息卡",
                    )
                )
                continue
            if not force and crud.has_content_entity_recognition(db, content.id):
                summary.skipped += 1
                summary.outcomes.append(
                    LinkOutcome(
                        content.id,
                        content.title,
                        "skipped",
                        reason="已有关系数据，跳过（--force 重算）",
                    )
                )
                continue

            try:
                payload = fetch(content.bangumi_id)
            except Exception as exc:  # 网络/解析失败只记 failed，不中断其余作品
                summary.failed += 1
                summary.outcomes.append(
                    LinkOutcome(
                        content.id,
                        content.title,
                        "failed",
                        reason=f"获取条目失败：{exc}",
                    )
                )
                continue

            recognition = entity_links.recognize(content.id, payload)

            series_note = ""
            if series:
                try:
                    related_payload = fetch_related(content.bangumi_id)
                except Exception as exc:  # 系列拉取失败不阻塞关系识别，仅注明
                    series_note = f"；关联条目获取失败（{exc}）"
                else:
                    name = series_from_related(db, content, related_payload)
                    if name and not recognition.series:
                        recognition.series = name

            if not recognition.has_data():
                summary.skipped += 1
                summary.outcomes.append(
                    LinkOutcome(
                        content.id,
                        content.title,
                        "skipped",
                        reason=f"未识别到作者/公司或系列，无需写入{series_note}",
                    )
                )
                continue

            if apply:
                entity_links.apply_recognition(db, recognition)
                summary.recognized += 1
                status, reason = "recognized", f"已写入{series_note}"
            else:
                summary.pending += 1
                status, reason = "pending", f"预览模式：未写入数据库{series_note}"
            if recognition.links:
                summary.linked += 1
            if recognition.series:
                summary.series_set += 1
            summary.outcomes.append(
                LinkOutcome(
                    content.id,
                    content.title,
                    status,
                    [
                        (link.name, link.entity_type, link.role)
                        for link in recognition.links
                    ],
                    recognition.series,
                    reason,
                )
            )
    finally:
        db.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "给已有作品回填「作者/公司关联 + 所属系列」"
            "（默认只预览不写库，--apply 才写入）。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把识别结果写入 content_entity_links / contents.series_id；不加则只预览",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有关系数据的作品也重新识别（infobox 解析规则更新后刷新用）",
    )
    parser.add_argument(
        "--content-id",
        type=int,
        nargs="+",
        default=None,
        metavar="N",
        help="只处理指定 ID 的作品（可写多个）；缺省处理全部作品",
    )
    parser.add_argument(
        "--series",
        action="store_true",
        help="同时补所属系列（走关联条目端点，每部作品多一次请求）",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="HTTP 代理（也可用 BANGUMI_PROXY 环境变量；缺省直连）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"失败重试次数（默认 {DEFAULT_RETRIES}）",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None, *, session_factory=None) -> int:
    """CLI 入口：执行一次关系回填。"""
    args = build_parser().parse_args(argv)

    if args.content_id is not None:
        if any(content_id <= 0 for content_id in args.content_id):
            print("用法错误：--content-id 必须是正整数")
            return EXIT_USAGE
        args.content_id = list(dict.fromkeys(args.content_id))  # 去重保序

    if session_factory is None:
        engine = build_engine(args.database_url)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    fetch, fetch_related = build_fetchers(
        proxy=resolve_proxy(args.proxy),
        timeout=args.timeout,
        retries=args.retries,
    )

    print(f"数据库：{args.database_url}")
    if args.content_id:
        print(f"范围：指定作品 {args.content_id}")
    if args.series:
        print("系列：启用（每部作品将多请求一次关联条目端点）")

    summary = run_backfill(
        session_factory,
        apply=args.apply,
        force=args.force,
        content_ids=args.content_id,
        series=args.series,
        fetch=fetch,
        fetch_related=fetch_related,
    )

    for outcome in summary.outcomes:
        print(outcome.describe())
    print(summary.describe())

    if args.content_id and not summary.requested:
        print(f"没有找到指定 ID 的作品：{args.content_id}")
        return EXIT_NO_MATCH
    if not args.apply:
        print("模式：dry-run，未写入数据库。确认后追加 --apply 正式回填。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
