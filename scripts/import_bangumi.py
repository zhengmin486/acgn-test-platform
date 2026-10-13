"""Bangumi 条目导入脚本：从 Bangumi 官方 API 拉取条目，映射为 Content 记录。

设计要点（给后续维护者）：
- 默认只预览：不写数据库；显式加 --apply 才正式入库。
- 代理可配置：--proxy 参数或 BANGUMI_PROXY 环境变量，默认直连（不读取系统代理）。
- 只用 Python 标准库（urllib），不新增第三方依赖。
- 重复导入安全：bangumi_id 已存在时直接跳过，不覆盖本地数据。
- 类型映射只支持 type=2（anime）、type=4（game）；书籍/音乐等类型明确跳过，
  不会错误归类为漫画或小说。
- rating 只取 API 返回的有效评分（rating.score > 0），取不到就跳过，不伪造。

用法示例：
    # 预览（默认，不写库）
    python scripts/import_bangumi.py 265 975
    # 正式入库
    python scripts/import_bangumi.py 265 975 --apply
    # 走本机代理
    python scripts/import_bangumi.py 265 --proxy http://127.0.0.1:9910
    # 或用环境变量
    set BANGUMI_PROXY=http://127.0.0.1:9910 && python scripts/import_bangumi.py 265

本文件同时是可复用模块：核心函数（map_subject / fetch_subject / run_import）
可被测试或后续功能直接调用，见 tests/test_bangumi_import.py。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError, SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402

# ---------- 常量与配置 ----------

BANGUMI_API_BASE = "https://api.bgm.tv"
SOURCE_URL_TEMPLATE = "https://bgm.tv/subject/{subject_id}"
DEFAULT_USER_AGENT = "acgn-test-platform/0.1 (local import script; github.com/acgn-test-platform)"
DEFAULT_TIMEOUT = 10.0  # 单次请求超时（秒）
DEFAULT_RETRIES = 2  # 失败后的重试次数（总请求次数 = 1 + retries）

# Bangumi 条目类型 -> 本站 Content.type；其余类型（1 书籍 / 3 音乐 / 5 三次元）明确跳过。
TYPE_MAPPING: dict[int, str] = {2: "anime", 4: "game"}
TYPE_NAMES: dict[int, str] = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 5: "三次元"}

# 这些 HTTP 状态码值得重试（限流或服务端临时故障）；4xx 其余状态直接失败。
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

TITLE_MAX_LENGTH = 200  # 与 Content.title 的 String(200) 一致


class BangumiAPIError(RuntimeError):
    """访问 Bangumi API 失败（网络错误、超时、HTTP 错误状态等）。"""


class SubjectMappingError(ValueError):
    """API 数据无法映射为本站 Content 记录（类型不支持、必要字段缺失等）。"""


# ---------- 数据结构 ----------


@dataclass
class MappedSubject:
    """映射完成后、等待入库的一条记录（字段与 Content 一一对应）。"""

    bangumi_id: int
    title: str
    type: str
    summary: str
    rating: float
    source_url: str


@dataclass
class SubjectOutcome:
    """单个 Bangumi ID 的处理结果。

    status 取值：
    - imported  已正式写入数据库
    - preview   预览模式：映射成功但未写入
    - duplicate bangumi_id 已存在，跳过（不覆盖本地数据）
    - skipped   类型不支持 / 必要数据缺失
    - failed    网络请求或数据库写入失败
    """

    bangumi_id: int
    status: str
    title: str = ""
    reason: str = ""

    def describe(self) -> str:
        """输出一行人类可读的结果说明。"""
        label = {
            "imported": "已导入",
            "preview": "可导入(预览)",
            "duplicate": "重复",
            "skipped": "跳过",
            "failed": "失败",
        }.get(self.status, self.status)
        parts = [f"[{label}] #{self.bangumi_id}"]
        if self.title:
            parts.append(f"《{self.title}》")
        if self.reason:
            parts.append(f"- {self.reason}")
        return " ".join(parts)


@dataclass
class ImportSummary:
    """一次导入运行的汇总统计。"""

    requested: int
    fetched: int
    imported: int
    preview: int
    duplicates: int
    skipped: int
    failed: int
    outcomes: list[SubjectOutcome] = field(default_factory=list)

    @property
    def importable(self) -> int:
        """可导入条数 = 已导入 + 预览中待导入。"""
        return self.imported + self.preview

    def describe(self) -> str:
        """汇总信息的多行文本。"""
        return "\n".join(
            [
                "导入汇总：",
                f"  请求条目：{self.requested}",
                f"  成功获取：{self.fetched}",
                f"  可导入：{self.importable}（正式导入 {self.imported}，预览未写入 {self.preview}）",
                f"  重复跳过：{self.duplicates}",
                f"  类型/数据不支持：{self.skipped}",
                f"  失败：{self.failed}",
            ]
        )


# ---------- 网络请求 ----------


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    """按代理配置构建 urllib opener。

    - proxy 非空：所有 http/https 请求走该代理；
    - proxy 为空：显式传入空的 ProxyHandler({})，不读取系统环境里的
      HTTP_PROXY 等变量，保证“默认直连”行为确定、可测试。
      （urllib 对空代理表不会注册任何代理方法，请求因此直连。）
    """
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def fetch_subject(
    subject_id: int,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    sleep=time.sleep,
) -> dict:
    """获取单个 Bangumi 条目的 JSON 数据。

    - 带明确的 User-Agent、超时和有限重试（限流/服务端故障/网络错误才重试）；
    - 全部尝试失败时抛 BangumiAPIError，错误信息包含条目 ID 与具体原因。
    sleep 参数用于重试间隔，测试时可注入为空函数避免真实等待。
    """
    url = f"{base_url.rstrip('/')}/v0/subjects/{int(subject_id)}"
    last_reason = "未知错误"

    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 5))  # 0.5s, 1s, 2s... 封顶 5s

        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
            )
            with opener.open(request, timeout=timeout) as response:
                body = response.read()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 失败：{last_reason}（{url}）"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_reason = f"网络错误：{exc}" if not isinstance(exc, TimeoutError) else "请求超时"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 失败：{last_reason}（{url}）"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_reason = f"响应不是有效 JSON：{exc}"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 失败：{last_reason}（{url}）"
            ) from exc

    raise BangumiAPIError(f"获取条目 {subject_id} 失败：{last_reason}（{url}）")


# ---------- 数据映射（纯函数，便于单独测试） ----------


def map_subject(payload: dict) -> MappedSubject:
    """把 Bangumi API 的条目 JSON 映射为可入库字段。

    规则：
    - 标题优先 name_cn，为空时回退 name；两者都为空则跳过；
    - summary 映射到摘要；为空则跳过（summary 是必填字段）；
    - rating 只取 rating.score 且必须 > 0，取不到就跳过（rating 必填，不伪造）；
    - type=2 -> anime，type=4 -> game；其余类型跳过（书籍/音乐不归类为漫画/小说）。
    无法映射时抛 SubjectMappingError，异常信息即跳过原因。
    """
    bangumi_id = payload.get("id")
    if not isinstance(bangumi_id, int) or bangumi_id <= 0:
        raise SubjectMappingError(f"缺少有效的条目 id（实际值：{payload.get('id')!r}）")

    bgm_type = payload.get("type")
    if bgm_type not in TYPE_MAPPING:
        type_name = TYPE_NAMES.get(bgm_type, f"未知类型 {bgm_type!r}")
        raise SubjectMappingError(
            f"暂不支持导入「{type_name}」类条目（本站目前只支持动画/游戏）"
        )

    title = (payload.get("name_cn") or "").strip() or (payload.get("name") or "").strip()
    if not title:
        raise SubjectMappingError("缺少可用标题（name_cn 与 name 均为空）")
    if len(title) > TITLE_MAX_LENGTH:
        raise SubjectMappingError(f"标题超过 {TITLE_MAX_LENGTH} 字符，无法安全存入 title 字段")

    summary = (payload.get("summary") or "").strip()
    if not summary:
        raise SubjectMappingError("缺少简介（summary 为空，本站 summary 为必填）")

    score = (payload.get("rating") or {}).get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score <= 0:
        raise SubjectMappingError(
            f"缺少有效评分（rating.score 实际值：{score!r}，本站 rating 为必填且不伪造评分）"
        )

    return MappedSubject(
        bangumi_id=bangumi_id,
        title=title,
        type=TYPE_MAPPING[bgm_type],
        summary=summary,
        rating=float(score),
        source_url=SOURCE_URL_TEMPLATE.format(subject_id=bangumi_id),
    )


# ---------- 入库流程 ----------


def _make_fetcher(
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float,
    retries: int,
    user_agent: str,
    base_url: str,
    sleep=time.sleep,
):
    """把 fetch_subject 的固定参数绑定为 fetch(subject_id) -> dict 的简单函数。"""

    def fetch(subject_id: int) -> dict:
        return fetch_subject(
            subject_id,
            opener=opener,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            base_url=base_url,
            sleep=sleep,
        )

    return fetch


def _map_and_store(db, payload: dict, *, apply: bool) -> SubjectOutcome:
    """对已成功获取的条目做映射、查重、入库，返回处理结果。

    - bangumi_id 已存在 -> duplicate，直接跳过不覆盖；
    - 预览模式 -> preview，不写库；
    - 正式模式写入失败（含唯一索引冲突、数据库错误）会回滚并归入对应结果，
      不中断其余条目的导入。
    """
    try:
        subject = map_subject(payload)
    except SubjectMappingError as exc:
        title = str(payload.get("name_cn") or payload.get("name") or "").strip()
        return SubjectOutcome(
            bangumi_id=payload.get("id") if isinstance(payload.get("id"), int) else -1,
            status="skipped",
            title=title,
            reason=str(exc),
        )

    try:
        existing_id = db.scalar(
            select(Content.id).where(Content.bangumi_id == subject.bangumi_id)
        )
        if existing_id is not None:
            return SubjectOutcome(
                bangumi_id=subject.bangumi_id,
                status="duplicate",
                title=subject.title,
                reason=f"该 bangumi_id 已存在（contents.id={existing_id}），不覆盖本地数据",
            )
        if not apply:
            return SubjectOutcome(
                bangumi_id=subject.bangumi_id,
                status="preview",
                title=subject.title,
                reason="预览模式：未写入数据库（加 --apply 正式导入）",
            )

        content = Content(
            title=subject.title,
            type=subject.type,
            summary=subject.summary,
            rating=subject.rating,
            bangumi_id=subject.bangumi_id,
            source_url=subject.source_url,
        )
        db.add(content)
        db.commit()
        return SubjectOutcome(
            bangumi_id=subject.bangumi_id,
            status="imported",
            title=subject.title,
            reason=f"已写入 contents.id={content.id}",
        )
    except IntegrityError:
        db.rollback()
        return SubjectOutcome(
            bangumi_id=subject.bangumi_id,
            status="duplicate",
            title=subject.title,
            reason="唯一索引 uq_contents_bangumi_id 冲突，已回滚",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        return SubjectOutcome(
            bangumi_id=subject.bangumi_id,
            status="failed",
            title=subject.title,
            reason=f"数据库错误：{exc}",
        )


def run_import(
    bangumi_ids,
    *,
    session_factory,
    apply: bool = False,
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    fetch=None,
    sleep=time.sleep,
) -> ImportSummary:
    """导入主流程：逐个获取 -> 映射 -> 查重/入库，返回汇总。

    参数说明：
    - session_factory：产生 SQLAlchemy Session 的工厂（如 SessionLocal），
      本函数负责打开与关闭会话；
    - fetch：可选的 fetch(subject_id) -> dict 注入点，测试用它替代真实 API；
      不传时按 proxy/timeout 等参数构造真实请求器。
    任何单条失败都不会中断整体流程，全部结果汇总在 ImportSummary.outcomes。
    """
    ids = [int(subject_id) for subject_id in bangumi_ids]
    if fetch is None:
        fetch = _make_fetcher(
            opener=build_opener(proxy),
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            base_url=base_url,
            sleep=sleep,
        )

    outcomes: list[SubjectOutcome] = []
    fetched = 0
    db = session_factory()
    try:
        for subject_id in ids:
            if subject_id <= 0:
                outcomes.append(
                    SubjectOutcome(
                        bangumi_id=subject_id,
                        status="failed",
                        reason="无效的 Bangumi ID（必须为正整数）",
                    )
                )
                continue
            try:
                payload = fetch(subject_id)
            except BangumiAPIError as exc:
                outcomes.append(
                    SubjectOutcome(bangumi_id=subject_id, status="failed", reason=str(exc))
                )
                continue
            fetched += 1
            outcomes.append(_map_and_store(db, payload, apply=apply))
    finally:
        db.close()

    return build_summary(requested=len(ids), fetched=fetched, outcomes=outcomes)


def build_summary(requested: int, fetched: int, outcomes: list[SubjectOutcome]) -> ImportSummary:
    """按结果状态归类计数，生成汇总对象。"""
    counts = {"imported": 0, "preview": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return ImportSummary(
        requested=requested,
        fetched=fetched,
        imported=counts["imported"],
        preview=counts["preview"],
        duplicates=counts["duplicate"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        outcomes=list(outcomes),
    )


# ---------- 命令行入口 ----------


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description="从 Bangumi 官方 API 导入条目到本站 contents 表（默认只预览，--apply 才写入）。",
    )
    parser.add_argument(
        "ids",
        nargs="+",
        type=int,
        metavar="BANGUMI_ID",
        help="一个或多个 Bangumi 条目 ID，例如：265 975",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="正式写入数据库；不加此参数则只预览，不写库",
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
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"失败重试次数（默认 {DEFAULT_RETRIES}）",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="请求使用的 User-Agent",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="数据库连接 URL；默认读取应用配置 DATABASE_URL（本地默认 SQLite）",
    )
    return parser


def resolve_proxy(explicit_proxy: str | None, env=None) -> str | None:
    """代理取值优先级：--proxy 参数 > BANGUMI_PROXY 环境变量 > 无（直连）。"""
    if env is None:
        env = os.environ
    return explicit_proxy or env.get("BANGUMI_PROXY") or None


def main(argv=None) -> int:
    """CLI 入口：解析参数并执行导入；有失败条目时返回非 0 退出码。"""
    args = build_parser().parse_args(argv)
    proxy = resolve_proxy(args.proxy)
    database_url = args.database_url or settings.database_url

    print(f"数据库：{database_url}")
    print(f"模式：{'正式导入（--apply）' if args.apply else '预览（不写数据库）'}")
    print(f"代理：{proxy if proxy else '未启用（直连）'}")

    engine = build_engine(database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        fetch = _make_fetcher(
            opener=build_opener(proxy),
            timeout=args.timeout,
            retries=args.retries,
            user_agent=args.user_agent,
            base_url=BANGUMI_API_BASE,
            sleep=time.sleep,
        )
        summary = run_import(
            args.ids,
            session_factory=session_factory,
            apply=args.apply,
            fetch=fetch,
        )
    finally:
        engine.dispose()

    for outcome in summary.outcomes:
        print(outcome.describe())
    print(summary.describe())
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
