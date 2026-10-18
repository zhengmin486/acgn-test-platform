"""封面回填脚本：给库里已有条目补下载 Bangumi 封面（image_url 为空的）。

适用场景：
- 封面上线前导入的作品没有本地封面（image_url 为空）；
- 某次导入时 --no-cover 或下载失败，事后补图；
- 封面存储目录迁移后重下一遍（--force）。

设计要点：
- 只更新 contents.image_url 一列，不触碰标题 / 简介 / 评分等任何其它字段；
- 默认只预览：真实下载图片文件（幂等，同路径覆盖），但不改数据库；
  显式加 --apply 才把 /covers/... 地址写进库；
- 网络层复用 import_bangumi（同一套 opener / 重试 / 魔数校验），
  不复制第二套下载逻辑；
- 单条失败不中断整体流程，汇总报告成功 / 失败 / 跳过数，退出码非 0
  表示有失败项（方便定时任务告警）。

用法示例：
    # 预览：会真实下图到本地，但不写库
    python scripts/fetch_covers.py
    # 正式回填（写 image_url）
    python scripts/fetch_covers.py --apply
    # 已有封面的也重下（封面目录迁移 / 换尺寸后刷新）
    python scripts/fetch_covers.py --apply --force
    # 走本机代理（与 import_bangumi 同一环境变量 BANGUMI_PROXY）
    set BANGUMI_PROXY=http://127.0.0.1:9910 && python scripts/fetch_covers.py --apply
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content, ContentFavorite, ContentViewHistory  # noqa: E402

# 复用导入脚本的网络层（fetch_subject / build_opener / download_cover 等）。
# scripts/ 不是包，按文件路径加载（与直接运行 import_bangumi.py 等价）。
_IMPORT_SCRIPT = PROJECT_ROOT / "scripts" / "import_bangumi.py"
_spec = importlib.util.spec_from_file_location("import_bangumi_for_fetch_covers", _IMPORT_SCRIPT)
import_bangumi = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = import_bangumi
_spec.loader.exec_module(import_bangumi)


@dataclass
class CoverOutcome:
    """单个条目的回填结果。"""

    content_id: int
    bangumi_id: int
    title: str
    status: str  # downloaded / applied / failed / skipped
    reason: str = ""

    def describe(self) -> str:
        label = {
            "downloaded": "已下图(预览)",
            "applied": "已回填",
            "failed": "失败",
            "skipped": "跳过",
        }.get(self.status, self.status)
        parts = [f"[{label}] contents.id={self.content_id}"]
        if self.bangumi_id:
            parts.append(f"(Bangumi #{self.bangumi_id})")
        parts.append(f"《{self.title}》")
        if self.reason:
            parts.append(f"- {self.reason}")
        return " ".join(parts)


@dataclass
class CoverSummary:
    """一次回填运行的汇总统计。"""

    scanned: int = 0
    downloaded: int = 0
    applied: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[CoverOutcome] = field(default_factory=list)

    def describe(self) -> str:
        return "\n".join(
            [
                "回填汇总：",
                f"  待处理条目：{self.scanned}",
                f"  下图成功：{self.downloaded}（写入库 {self.applied}）",
                f"  跳过：{self.skipped}",
                f"  失败：{self.failed}",
            ]
        )


def _engaged_ids(db) -> set[int]:
    """有互动的作品 id：被收藏 / 标注 / 浏览过（多用户取并集）。

    标注已并入收藏行（content_favorites），只需扫收藏与浏览记录两张表。
    """
    ids: set[int] = set()
    for model in (ContentFavorite, ContentViewHistory):
        ids.update(int(value) for value in db.scalars(select(model.content_id).distinct()))
    return ids


def list_pending_contents(
    db,
    *,
    force: bool,
    scope: str = "engaged",
    top: int = 24,
    limit: int | None = None,
) -> list[Content]:
    """挑出需要回填封面的条目。

    只处理有 bangumi_id 且（无 image_url 或 --force）的条目；再用 scope 收窄：
    - ``engaged``（默认）：只补有互动的作品（收藏 / 标注 / 浏览），最省资源；
    - ``top``：互动作品 + 评分最高的 ``top`` 部（首页推荐、详情页相关作品会用到）；
    - ``all``：全部，慎用（上千条会很慢）。
    """
    stmt = select(Content).where(Content.bangumi_id.is_not(None)).order_by(Content.id)
    rows = [row for row in db.scalars(stmt) if force or not row.image_url]
    if scope == "all":
        pass
    else:
        allowed = _engaged_ids(db)
        if scope == "top" and top > 0:
            allowed |= set(
                int(value)
                for value in db.scalars(
                    select(Content.id)
                    .where(Content.bangumi_id.is_not(None))
                    .order_by(Content.rating.desc(), Content.id.asc())
                    .limit(top)
                )
            )
        rows = [row for row in rows if row.id in allowed]
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def run_backfill(
    session_factory,
    *,
    apply: bool = False,
    force: bool = False,
    scope: str = "engaged",
    top: int = 24,
    limit: int | None = None,
    pause: float = 0.0,
    proxy: str | None = None,
    timeout: float = import_bangumi.DEFAULT_TIMEOUT,
    retries: int = import_bangumi.DEFAULT_RETRIES,
    user_agent: str = import_bangumi.DEFAULT_USER_AGENT,
    fetch=None,
    cover_downloader=None,
    sleep=time.sleep,
) -> CoverSummary:
    """回填主流程：逐条拉条目 -> 提取封面地址 -> 下载 -> （可选）写库。

    注入点（测试用）：
    - fetch：可选的 fetch(subject_id) -> dict，替代真实 Bangumi API；
    - cover_downloader：可选的 download(remote_url, bangumi_id) -> 站内地址，
      替代真实下载；默认按配置构造。
    任何单条失败不中断整体流程。
    """
    opener = import_bangumi.build_opener(proxy)
    if fetch is None:
        fetch = import_bangumi._make_fetcher(
            opener=opener,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            base_url=import_bangumi.BANGUMI_API_BASE,
            sleep=sleep,
        )
    if cover_downloader is None:
        cover_downloader = import_bangumi._make_cover_downloader(
            opener=opener,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            sleep=sleep,
        )

    summary = CoverSummary()
    db = session_factory()
    try:
        for index, content in enumerate(
            list_pending_contents(db, force=force, scope=scope, top=top, limit=limit)
        ):
            if index and pause > 0:
                sleep(pause)
            summary.scanned += 1
            try:
                payload = fetch(content.bangumi_id)
            except import_bangumi.BangumiAPIError as exc:
                summary.failed += 1
                summary.outcomes.append(
                    CoverOutcome(content.id, content.bangumi_id, content.title, "failed", str(exc))
                )
                continue

            remote_url = import_bangumi.extract_cover_url(payload)
            if not remote_url:
                summary.skipped += 1
                summary.outcomes.append(
                    CoverOutcome(
                        content.id,
                        content.bangumi_id,
                        content.title,
                        "skipped",
                        "该条目没有可用封面图",
                    )
                )
                continue

            try:
                local_url = cover_downloader(remote_url, content.bangumi_id)
            except import_bangumi.CoverDownloadError as exc:
                summary.failed += 1
                summary.outcomes.append(
                    CoverOutcome(content.id, content.bangumi_id, content.title, "failed", str(exc))
                )
                continue

            summary.downloaded += 1
            if apply:
                content.image_url = local_url
                db.commit()
                summary.applied += 1
                summary.outcomes.append(
                    CoverOutcome(
                        content.id, content.bangumi_id, content.title, "applied", local_url
                    )
                )
            else:
                summary.outcomes.append(
                    CoverOutcome(
                        content.id, content.bangumi_id, content.title, "downloaded", local_url
                    )
                )
    finally:
        db.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "给已有 contents 回填 Bangumi 封面（默认只预览不写库，--apply 才写 image_url）。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把封面站内地址写入 contents.image_url；不加则只下图不改库",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有封面的条目也重新下载（刷新 / 迁移封面目录后用）",
    )
    parser.add_argument(
        "--scope",
        choices=("engaged", "top", "all"),
        default="engaged",
        help="补图范围：engaged=有互动（收藏/标注/浏览，默认）；top=互动+评分最高 N 部；all=全部",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=24,
        help="scope=top 时额外补评分最高的多少部（默认 24，覆盖首页推荐与相关作品）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本次最多处理多少部（分批慢慢补时用）",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="每部之间的暂停秒数（默认 0.5，限速避免打爆图床）",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="HTTP(S) 代理地址，如 http://127.0.0.1:9910；也可用环境变量 BANGUMI_PROXY；默认直连",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=import_bangumi.DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（默认 {import_bangumi.DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=import_bangumi.DEFAULT_RETRIES,
        help=f"失败重试次数（默认 {import_bangumi.DEFAULT_RETRIES}）",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="数据库连接串（默认取配置 / .env）",
    )
    return parser


def main(argv=None, *, fetch=None, cover_downloader=None) -> int:
    """CLI 入口：执行一次封面回填；有失败项返回非 0 退出码。"""
    args = build_parser().parse_args(argv)
    proxy = import_bangumi.resolve_proxy(args.proxy)

    print(f"数据库：{args.database_url}")
    print(f"模式：{'正式回填（--apply）' if args.apply else '预览（只下图，不写库）'}")
    print(f"代理：{proxy if proxy else '未启用（直连）'}")
    print(f"封面目录：{settings.cover_storage_dir}")

    engine = build_engine(args.database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        summary = run_backfill(
            session_factory,
            scope=args.scope,
            top=args.top,
            limit=args.limit,
            pause=args.sleep,
            apply=args.apply,
            force=args.force,
            proxy=proxy,
            timeout=args.timeout,
            retries=args.retries,
            fetch=fetch,
            cover_downloader=cover_downloader,
        )
    finally:
        engine.dispose()

    for outcome in summary.outcomes:
        print(outcome.describe())
    print(summary.describe())
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
