"""热门选品器：从 bgm.tv 分区首页抓候选条目，按热度分层输出导入计划。

为什么需要这个（2026-10-18）：
    v0 API 的排行榜端点（``/v0/rank/subject/{type}/{sort}``）实测**全部 404**，
    用户收藏接口要登录 token，nickname 接口也不存在——整个 v0 只剩
    ``/subjects/{id}`` 与 ``/subjects/{id}/subjects`` 两个可用。所以「取热门榜」
    这件事在 API 侧无法完成，只能走网页版：``https://bgm.tv/anime`` 等分区首页
    返回的是当前热门条目，``?page=N`` 可翻页。

    另外实测过两条替代路线都不行：
    - 随机探测条目 ID（1..60000 采样 12 个）：收藏 >5000 的**零部**，全是冷门；
    - 从种子关联扩散（8 部种子）：只产出 125 个候选，且塞满手游 / Q 版 /
      4D 衍生作，收藏量个位数——那是「同系列周边」不是「热门榜」。

分层依据是详情接口的 ``collection.collect`` 与 ``rating.score``。关联列表只有
id/type/name/images 不含这两项，所以**必须逐条取详情才能分层**，不存在「先筛后抓」。

封面策略（重要）：
    低权重条目**不批量下封面**，只在库里记下 Bangumi 原始图 URL，由详情页在
    用户真正打开时按需下载（见 app/media.py 的 ensure_variant / 落库流程）。
    这样既省本机下载与上行带宽，又不会留下「长尾永远没封面」的空洞——只要有人
    访问，对应封面就会补齐。全部批量下完 1000 张约 68 MB，按需下载则只付
    实际被访问的那部分。

只用标准库（urllib / re / json / argparse），不新增依赖。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_bangumi import (  # noqa: E402
    BANGUMI_API_BASE,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    TYPE_MAPPING,
    BangumiAPIError,
    build_opener,
    resolve_content_type,
)

# bgm.tv 分区 -> 会出现的 Bangumi 条目类型。
# 注意「书籍」分区里小说(type=1)与漫画(type=6)混在一起，所以一个分区对应多个 type。
#
# 修正（2026-10-21）：上面这句是旧注释，编码写反了。实测真实语义是
#   type=1 书籍（漫画与小说**按 platform 拆**）、type=2 动画、type=3 音乐、
#   type=4 游戏、type=6 真人剧（Archive README 把 6 写成「漫画」是错的）。
# 所以本模块**不能只查 TYPE_MAPPING**（它只有 2/4 两项）：那样 book 分区抓到的
# 书籍条目会被整类丢掉，novel/comic 配额恒为 0、余额全溢出给动画。
# 类型判定统一走 import_bangumi.resolve_content_type，与入库口径保持一致。
BROWSER_CATEGORIES: tuple[str, ...] = ("anime", "book", "game")
SUBJECT_LINK_RE = re.compile(r"/subject/(\d+)")

# 浏览器的排序方式。实测：
#   sort=trends 近期注目、sort=rank 综合评分，两者内容不同，合起来覆盖更广。
#   sort=date 会返回空列表，不要用。
BROWSER_SORTS: tuple[str, ...] = ("trends", "rank")

# 自然比例配额（用户 2026-10-18 定）：贴近 ACG 站点的真实构成，
# 避免为了「四等分」去硬拉长尾导致评分整体被拉低。
QUOTA_ANIME = 0.50
QUOTA_GAME = 0.20
QUOTA_NOVEL = 0.18
QUOTA_COMIC = 0.12

# 热度分层阈值（collection.collect 优先，score 作为补充信号）。
TIER_S_COLLECT = 20000
TIER_A_COLLECT = 8000
TIER_B_COLLECT = 3000

PAGE_SLEEP = 0.4
SUBJECT_SLEEP = 0.35


@dataclass
class Candidate:
    """一条候选作品：ID + 分区来源 + 详情指标 + 分层结果。"""

    bangumi_id: int
    source_category: str
    subject_type: int | None = None
    # 抓详情时定好的本站类型。书籍(type=1)光看 subject_type 分不出漫画/小说，
    # 必须靠 payload 的 platform / infobox 判，判完落在这里。
    resolved_type: str | None = None
    collect: int = 0
    score: float = 0.0
    title: str = ""
    image_medium: str = ""
    image_large: str = ""
    has_infobox: bool = False
    tag_count: int = 0
    summary_len: int = 0
    detail_ok: bool = False

    @property
    def content_type(self) -> str | None:
        return self.resolved_type or (
            TYPE_MAPPING.get(self.subject_type) if self.subject_type else None
        )

    @property
    def tier(self) -> str:
        """按热度分层：S > A > B > C。收藏数是主信号，评分用于同档微调。"""
        if self.collect >= TIER_S_COLLECT:
            return "S"
        if self.collect >= TIER_A_COLLECT:
            return "A"
        if self.collect >= TIER_B_COLLECT:
            return "B"
        # 收藏不高但评分很亮的冷门高分作也算 B，省得被埋掉
        if self.score >= 8.5:
            return "B"
        return "C"

    @property
    def need_cover_now(self) -> bool:
        """是否在本次批量下载封面。

        S/A 全下，B 下（数量可控），C 不下——改为详情页按需下载，
        省下本机下载与上行带宽，又不留永久空洞。
        """
        return self.tier in ("S", "A", "B")

    def describe(self) -> str:
        return (
            f"[{self.tier}] #{self.bangumi_id} {self.content_type or '?'} "
            f"收藏{self.collect} 评分{self.score} {self.title[:20]}"
        )


@dataclass
class SelectionReport:
    """选品结果汇总，供 CLI 打印与 JSON 导出。"""

    candidates: list[Candidate] = field(default_factory=list)
    pages_fetched: int = 0
    detail_failures: list[int] = field(default_factory=list)
    raw_ids: int = 0

    def by_type(self) -> Counter:
        return Counter(c.content_type for c in self.candidates)

    def by_tier(self) -> Counter:
        return Counter(c.tier for c in self.candidates)


def _build_headers() -> dict[str, str]:
    """网页版的 UA 要求很松（连 ``bangumi-go/1.0`` 都通），但给个像样的浏览器 UA 更稳。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }


def fetch_category_ids(
    category: str,
    pages: int,
    *,
    opener: urllib.request.OpenerDirector,
    base_url: str = "https://bgm.tv",
    timeout: float = DEFAULT_TIMEOUT,
    sorts: tuple[str, ...] = BROWSER_SORTS,
    sleep=time.sleep,
) -> tuple[list[int], int]:
    """抓某分区的浏览页，返回 (条目 ID 列表, 成功请求数)。

    **必须走 ``/{category}/browser`` 而不是 ``/{category}``**：实测分区首页
    （``/anime``）的 ``?page=N`` 与 ``?sort=`` 参数**全部被忽略**——翻 6 页
    返回的还是同一批 88 个 ID；只有 ``/anime/browser?sort=trends&page=2``
    才真正翻页换内容（实测 8 页累计 167 个且仍在涨）。

    另外 ``/anime/browser/``（带尾斜杠）会 403，不带尾斜杠正常。
    """
    ids: list[int] = []
    ok_requests = 0
    for sort in sorts:
        for page in range(1, max(1, pages) + 1):
            url = f"{base_url.rstrip('/')}/{category}/browser?sort={sort}&page={page}"
            text = ""
            # bgm.tv 会主动断开长连接（实测 WinError 10054），单页要能重试，
            # 否则一次抖动就会让整轮少掉几十个候选。
            for attempt in range(3):
                try:
                    request = urllib.request.Request(url, headers=_build_headers())
                    with opener.open(request, timeout=timeout) as response:
                        text = response.read().decode("utf-8", "replace")
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if attempt == 2:
                        print(
                            f"  分区 {category} sort={sort} 第 {page} 页抓取失败：{exc}",
                            file=sys.stderr,
                        )
                        text = ""
                        break
                    if sleep:
                        sleep(1.0 + attempt)
            if not text:
                if page == 1:
                    break  # 整个排序不可用，换下一个 sort
                continue

            unique = list(dict.fromkeys(int(m) for m in SUBJECT_LINK_RE.findall(text)))
            if not unique:
                break  # 翻到头了
            for sid in unique:
                if sid not in ids:
                    ids.append(sid)
            ok_requests += 1
            print(
                f"  分区 {category} sort={sort} 第 {page} 页："
                f"{len(unique)} 个（累计 {len(ids)}）"
            )
            if sleep:
                sleep(PAGE_SLEEP)
    return ids, ok_requests


def fetch_detail(
    subject_id: int,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 3,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    sleep=time.sleep,
) -> dict:
    """取单条详情（复用 import_bangumi 的重试语义，失败抛 BangumiAPIError）。

    默认重试 3 次：bgm.tv 会主动断连（WinError 10054），一次失败就丢条目太浪费。
    """
    url = f"{base_url.rstrip('/')}/v0/subjects/{int(subject_id)}"
    last = "未知错误"
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
            )
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            # 404 是「条目不存在」，重试没意义；5xx 才值得再试
            if exc.code not in (500, 502, 503, 504) or attempt >= retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last = str(exc)
            if attempt >= retries:
                break
        if sleep:
            sleep(1.0 + attempt)
    raise BangumiAPIError(f"获取条目 {subject_id} 详情失败：{last}")


def collect_candidates(
    categories: tuple[str, ...] = BROWSER_CATEGORIES,
    pages_per_category: int = 6,
    limit: int = 0,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    fetch_detail_fn=None,
    sleep=time.sleep,
) -> SelectionReport:
    """抓候选并逐条取详情分层，返回 SelectionReport。

    - ``opener`` 复用同一个（带代理的）opener，抓网页与调 API 都走它；
    - ``fetch_detail_fn`` 默认用本模块的 fetch_detail，测试可注入桩；
    - ``limit`` > 0 时限制详情请求数，便于小批试点。
    """
    opener = opener or build_opener(None)
    fetch_detail_fn = fetch_detail_fn or fetch_detail
    report = SelectionReport()

    pool: dict[int, str] = {}
    for category in categories:
        print(f"[选品] 抓取分区 {category}/browser ...")
        ids, ok = fetch_category_ids(
            category, pages_per_category, opener=opener, sleep=sleep
        )
        report.pages_fetched += ok
        for sid in ids:
            pool.setdefault(sid, category)
    report.raw_ids = len(pool)
    print(f"[选品] 候选池 {len(pool)} 个唯一 ID（{report.pages_fetched} 次成功请求）")

    fetched = 0
    for sid, category in pool.items():
        if limit and fetched >= limit:
            break
        try:
            payload = fetch_detail_fn(sid, opener=opener, sleep=sleep)
        except BangumiAPIError:
            report.detail_failures.append(sid)
            continue

        # 走 resolve_content_type 而不是 TYPE_MAPPING：书籍按 platform / infobox
        # 拆成 comic / novel，非日本小说与画集会被判为 None 丢掉——与入库口径一致。
        content_type = resolve_content_type(payload)
        if content_type is None:
            continue  # 音乐 / 真人剧 / 非日本书 / 画集，本站不收

        rating = payload.get("rating") or {}
        collection = payload.get("collection") or {}
        images = payload.get("images") or {}
        title = (payload.get("name_cn") or payload.get("name") or "").strip()
        if not title:
            continue

        report.candidates.append(
            Candidate(
                bangumi_id=sid,
                source_category=category,
                subject_type=payload.get("type"),
                resolved_type=content_type,
                collect=int(collection.get("collect") or 0),
                score=float(rating.get("score") or 0.0),
                title=title,
                image_medium=images.get("medium") or images.get("large") or "",
                image_large=images.get("large") or "",
                has_infobox=bool(payload.get("infobox")),
                tag_count=len(payload.get("tags") or []),
                summary_len=len(payload.get("summary") or ""),
                detail_ok=True,
            )
        )
        fetched += 1
        if fetched % 50 == 0:
            print(f"  已取详情 {fetched} 个 ...")
        if sleep:
            sleep(SUBJECT_SLEEP)

    return report


def allocate(
    report: SelectionReport,
    total: int = 1000,
    quotas: dict[str, float] | None = None,
) -> list[Candidate]:
    """按配额抽取，每类内部按热度从高到低排。

    热度排序让 S/A 层作品优先占据配额，天然实现「权重高的先做全信息」。
    配额取不满时（该类型候选不够）就把余额让给其它类型，尽量凑满 total。
    """
    quotas = quotas or {
        "anime": QUOTA_ANIME,
        "game": QUOTA_GAME,
        "novel": QUOTA_NOVEL,
        "comic": QUOTA_COMIC,
    }
    by_type: dict[str, list[Candidate]] = {}
    for cand in report.candidates:
        key = cand.content_type
        if key:
            by_type.setdefault(key, []).append(cand)

    def heat_key(c: Candidate) -> tuple:
        # 收藏数优先降序，评分次之，ID 兜底保证结果稳定可复现
        return (-c.collect, -c.score, c.bangumi_id)

    for items in by_type.values():
        items.sort(key=heat_key)

    picked: list[Candidate] = []
    taken: set[int] = set()

    for content_type, share in quotas.items():
        want = int(total * share)
        pool = by_type.get(content_type, [])
        for cand in pool:
            if len([p for p in picked if p.content_type == content_type]) >= want:
                break
            if cand.bangumi_id in taken:
                continue
            picked.append(cand)
            taken.add(cand.bangumi_id)

    # 配额没吃满就用剩余候选补齐（仍按热度排）
    if len(picked) < total:
        rest = sorted(
            (c for c in report.candidates if c.bangumi_id not in taken),
            key=heat_key,
        )
        for cand in rest:
            if len(picked) >= total:
                break
            picked.append(cand)
            taken.add(cand.bangumi_id)

    return picked[:total]


def summarize(report: SelectionReport, picked: list[Candidate]) -> str:
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(
        f"候选池：抓取 {report.raw_ids} 个 ID，取到详情 {len(report.candidates)} 部"
        f"（失败 {len(report.detail_failures)}）"
    )
    lines.append("按类型：" + "  ".join(f"{k}={v}" for k, v in sorted(report.by_type().items())))
    lines.append("分层：" + "  ".join(f"{k}={v}" for k, v in sorted(report.by_tier().items())))
    lines.append("-" * 62)
    lines.append(f"本次选中：{len(picked)} 部")
    picked_types = Counter(c.content_type for c in picked)
    lines.append("选中按类型：" + "  ".join(f"{k}={v}" for k, v in sorted(picked_types.items())))
    picked_tiers = Counter(c.tier for c in picked)
    lines.append("选中分层：" + "  ".join(f"{k}={v}" for k, v in sorted(picked_tiers.items())))
    need_cover = sum(1 for c in picked if c.need_cover_now)
    lines.append(f"本次批量下封面：{need_cover} 部；其余 {len(picked) - need_cover} 部按需下载")
    lines.append("=" * 62)
    for cand in picked[:15]:
        lines.append("  " + cand.describe())
    if len(picked) > 15:
        lines.append(f"  ...（其余 {len(picked) - 15} 部略）")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 bgm.tv 分区首页抓热门候选，按热度分层并按配额抽取",
    )
    parser.add_argument(
        "--pages", type=int, default=8, help="每个分区每个排序翻多少页（默认 8）"
    )
    parser.add_argument("--total", type=int, default=1000, help="抽取总数（默认 1000）")
    parser.add_argument("--limit", type=int, default=0, help="最多取多少条详情（0=不限），试点时用")
    parser.add_argument("--proxy", default=None, help="代理；默认读 BANGUMI_PROXY")
    parser.add_argument("--out", default=None, help="把选中清单写成 JSON")
    parser.add_argument("--payloads", default=None, help="把选中条目的详情 payload 一并导出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    proxy = args.proxy or os.environ.get("BANGUMI_PROXY") or None
    print(f"代理：{proxy if proxy else '未启用（直连）'}")

    opener = build_opener(proxy)
    report = collect_candidates(
        pages_per_category=args.pages, limit=args.limit, opener=opener
    )
    if not report.candidates:
        print("没有取到任何可用候选。", file=sys.stderr)
        return 1

    picked = allocate(report, total=args.total)
    print(summarize(report, picked))

    if args.out:
        payload = [
            {
                "bangumi_id": c.bangumi_id,
                "content_type": c.content_type,
                "tier": c.tier,
                "collect": c.collect,
                "score": c.score,
                "title": c.title,
                "image_medium": c.image_medium,
                "image_large": c.image_large,
                "has_infobox": c.has_infobox,
                "need_cover_now": c.need_cover_now,
            }
            for c in picked
        ]
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"清单已写出：{args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())