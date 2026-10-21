"""档期 → 作品库 自动补库：新番档期里有、作品库里没有的作品自动入库。

设计要点（与项目既有约定的关系）：
- 档期条目（anime_schedules）**没有 Bangumi 条目 ID**——源站 yuc.wiki 档期页
  没有稳定的作品 ID，条目按「规范化主标题」做键（见 app/models.py AnimeSchedule）。
  所以不能按 ID 直接差集：先用镜像站搜索把「档期标题」解析成 Bangumi subject，
  再按 contents.bangumi_id 判断是否已在库。
- 解析置信度靠两条信号：标题精确匹配（日文原名优先，Bangumi 主名是日文，
  档期表同时存了 title_jp）+ 播出日期落在该档期窗口内（能区分正片与 OVA、
  区分同名不同季）。低于置信阈值一律不入库，宁缺勿错。
- 入库走 scripts.import_bangumi._map_and_store，与批量入库同一条管线
  （书籍自动拆分、标签/公司/年份分流、查重都在这里面），本模块不复制该逻辑。
- base_url 默认走镜像：官方 api.bgm.tv 在服务器不可达（Network unreachable，
  见项目记忆），可用环境变量 BANGUMI_API_BASE 覆盖。
- 只补「档期里有」的作品，不改变「作品不自动盲导入」的既定口径
  （run_sync.py 里 Bangumi 条目仍需显式给 ID）。
- **档期 ↔ 作品库关联**（--link-only）：档期译名（YUC）与作品译名（Bangumi）
  常常不同，按标题搜索会空手而归。这里用「Bangumi subject_id」做确定性关联
  键（--link-only 优先用标题归一匹配，命中即反填 subject_id，无需联网），
  并把档期的中/日译名并入作品 aliases，让全站搜索用任一译名都能搜到。

用法：
    python scripts/fill_schedule_works.py --season 202610            # 预览（不写库）
    python scripts/fill_schedule_works.py --season 202610 --apply     # 正式入库
    python scripts/fill_schedule_works.py --link-only                 # 预览关联回填
    python scripts/fill_schedule_works.py --link-only --apply         # 正式关联回填
    python scripts/fill_schedule_works.py --link-only --season 202610 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，项目根目录需在 sys.path 才能导入 app / scripts 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import crud  # noqa: E402
from app.models import AnimeSchedule, Content  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    _map_and_store,
    build_opener,
    fetch_subject,
)

# 官方 api.bgm.tv 在服务器不可达，默认改走镜像；显式设置 BANGUMI_API_BASE 可覆盖。
DEFAULT_MIRROR = "https://bgmapi.anibt.net"
SEARCH_PATH = "/v0/search/subjects"

# 置信度权重：标题精确匹配最强，档期日期命中次之。
SCORE_JP_EXACT = 100
SCORE_JP_PARTIAL = 50
SCORE_CN_EXACT = 80
SCORE_CN_PARTIAL = 40
SCORE_DATE_IN_SEASON = 40
SCORE_DATE_NEAR_SEASON = 15
# 接受匹配的最低分：低于此值不入库（宁缺勿错）。
MIN_SCORE = 80

# 单次运行最多处理多少条档期条目（防止异常档期把 cron 拖死）。
# 真实单季约 50~70 条（202610 实测 69 条），取 150 留足余量且仍是有界值。
MAX_ENTRIES = 150
# 关联回填一次最多处理多少条档期条目：全站历史档期累计上千条，
# 关联是纯本地操作（标题索引 + 精确查库），不需要像联网补库那样严格限流，
# 但仍保留上限防止一次性改写过多行。
MAX_LINK_ENTRIES = 2000
# 连续搜索失败多少次就整轮中止（镜像整体不可用时别逐条重试）。
ABORT_AFTER_ERRORS = 3
# 请求间的基础间隔，对镜像站礼貌一点。
DEFAULT_SLEEP = 0.3


def resolve_base_url(env=None) -> str:
    """Bangumi API 基址：环境变量优先，未设置时退回可达的镜像站。"""
    raw = (os.environ.get("BANGUMI_API_BASE") or "").strip()
    return raw.rstrip("/") if raw else DEFAULT_MIRROR


@dataclass
class FillSummary:
    """一次补库的统计结果。"""

    season_code: str = ""
    total: int = 0  # 档期条目数
    already: int = 0  # 已在作品库（按 bangumi_id 命中）
    imported: int = 0  # 本次新增入库
    skipped: int = 0  # 入库时按规则跳过（类型不支持等）
    unresolved: int = 0  # 搜索不到 / 置信不足
    failed: int = 0  # 网络或入库失败
    pending: int = 0  # 预览模式下「会入库」的条数
    capped: int = 0  # 因单次上限未处理的条数（>0 说明这一轮没跑完）
    aborted: str = ""  # 整轮中止的原因（空=未中止）
    details: list = field(default_factory=list)

    def add_detail(self, title: str, status: str, reason: str = "") -> None:
        if len(self.details) < 50:
            self.details.append({"title": title, "status": status, "reason": reason})

    @property
    def message(self) -> str:
        """一行人类可读的补库结论（并入同步任务的运行记录）。"""
        if self.aborted:
            return f"自动补库中止：{self.aborted}"
        parts = [
            f"档期 {self.season_code} 共 {self.total} 条",
            f"已在库 {self.already}",
        ]
        if self.pending:
            parts.append(f"可补 {self.pending}")
        if self.imported:
            parts.append(f"新增入库 {self.imported}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.unresolved:
            parts.append(f"未解析 {self.unresolved}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.capped:
            parts.append(f"超上限未处理 {self.capped}")
        prefix = "自动补库（预览）" if self.pending and not self.imported else "自动补库"
        return f"{prefix}：" + "、".join(parts)


@dataclass
class LinkSummary:
    """一次「档期 ↔ 作品库关联」的统计结果。"""

    total: int = 0  # 处理的档期条目数
    linked: int = 0  # 本次建立/更新了关联的条目数
    already: int = 0  # 之前已关联、这次无需改动
    unmatched: int = 0  # 找不到对应作品（作品库里确实没有）
    failed: int = 0  # 网络或写库失败
    skipped: int = 0  # 因单次上限未处理的条数
    aborted: str = ""  # 整轮中止的原因（空=未中止）
    details: list = field(default_factory=list)

    def add_detail(self, title: str, status: str, reason: str = "") -> None:
        if len(self.details) < 50:
            self.details.append({"title": title, "status": status, "reason": reason})

    @property
    def message(self) -> str:
        """一行人类可读的关联结论（并入同步任务的运行记录）。"""
        if self.aborted:
            return f"档期关联中止：{self.aborted}"
        parts = [
            f"档期条目 {self.total} 条",
            f"已关联 {self.linked}",
        ]
        if self.already:
            parts.append(f"此前已关联 {self.already}")
        if self.unmatched:
            parts.append(f"未匹配 {self.unmatched}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.skipped:
            parts.append(f"超上限未处理 {self.skipped}")
        prefix = "档期关联（预览）" if self.linked and not self.already else "档期关联"
        return f"{prefix}：" + "、".join(parts)


def season_window(season_code: str) -> tuple[date, date] | None:
    """档期码（YYYYMM）→ 该季播出窗口（起止日期）。

    新番按 1/4/7/10 月开播，每季覆盖 3 个自然月；档期码不是这四个月时，
    按所在季度归位（如 202611 归到 2026-10~12）。解析失败返回 None。
    """
    code = (season_code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    year, month = int(code[:4]), int(code[4:6])
    if not 1 <= month <= 12:
        return None
    start_month = ((month - 1) // 3) * 3 + 1
    start = date(year, start_month, 1)
    next_year, next_month = (year + 1, 1) if start_month == 10 else (year, start_month + 3)
    end = date(next_year, next_month, 1) - timedelta(days=1)
    return start, end


def compose_fallback_summary(entry) -> str:
    """档期条目有、对应 Bangumi 条目却无简介时，用档期自带信息拼一句兜底简介。

    仅用于补库路径（_map_and_store 的 summary_fallback），不进入主批量入库；
    Content.summary 是必填列，占位也不能为空，故这里始终产出非空文本。
    """
    work_type = (getattr(entry, "work_type", None) or "").strip()
    work_tags = (getattr(entry, "work_tags", None) or "").strip()
    title = (
        getattr(entry, "title_cn", None)
        or getattr(entry, "title_main", None)
        or getattr(entry, "title_jp", None)
        or "本作"
    )
    header = "".join([f"【{work_type}】" if work_type else "", work_tags])
    if header:
        return f"{header} {title}（Bangumi 暂无官方简介，信息来自新番档期）"
    return f"{title}（Bangumi 暂无官方简介，信息来自新番档期）"


def normalize_title(text: str | None) -> str:
    """标题归一化：去空白/标点/大小写，用于标题比对。"""
    if not text:
        return ""
    cleaned = re.sub(r"[\s　]+", "", str(text))
    cleaned = re.sub(r"[!！?？。.,，、·・:：;；~〜\-—_'\"“”‘’()（）\[\]【】]", "", cleaned)
    return cleaned.lower()


def parse_air_date(value) -> date | None:
    """把 Bangumi 的 date 字段（如 ``2020-01-09``）解析为日期。"""
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def search_subjects(
    keyword: str,
    *,
    opener,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep=time.sleep,
) -> list[dict]:
    """按关键词搜索 Bangumi 条目（动画），返回候选列表。

    Bangumi v0 的搜索是 POST 接口（GET 会 405），故这里走 POST；
    失败抛异常，由调用方决定重试或计入失败。
    """
    url = f"{base_url.rstrip('/')}{SEARCH_PATH}"
    body = json.dumps({"keyword": keyword, "filter": {"type": [2]}}).encode("utf-8")
    last_reason = "未知错误"

    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 5))
        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "User-Agent": user_agent,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return list(payload.get("data") or [])
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code}"
            if exc.code >= 500 and attempt < retries:
                continue
            raise RuntimeError(f"搜索「{keyword}」失败：{last_reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_reason = f"网络错误：{exc}"
            if attempt < retries:
                continue
            raise RuntimeError(f"搜索「{keyword}」失败：{last_reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_reason = f"响应不是有效 JSON：{exc}"
            if attempt < retries:
                continue
            raise RuntimeError(f"搜索「{keyword}」失败：{last_reason}") from exc

    raise RuntimeError(f"搜索「{keyword}」失败：{last_reason}")


def score_candidate(
    candidate: dict,
    *,
    jp: str,
    cn: str,
    window: tuple[date, date] | None,
) -> int:
    """给候选条目打分：标题匹配 + 播出日期是否落在该档期。"""
    name = normalize_title(candidate.get("name"))
    name_cn = normalize_title(candidate.get("name_cn"))
    score = 0

    if jp and name:
        if name == jp:
            score += SCORE_JP_EXACT
        elif jp in name or name in jp:
            score += SCORE_JP_PARTIAL
    if cn and name_cn:
        if name_cn == cn:
            score += SCORE_CN_EXACT
        elif cn in name_cn or name_cn in cn:
            score += SCORE_CN_PARTIAL

    air_date = parse_air_date(candidate.get("date"))
    if air_date and window:
        start, end = window
        if start <= air_date <= end:
            score += SCORE_DATE_IN_SEASON
        elif abs((air_date - start).days) <= 60 or abs((air_date - end).days) <= 60:
            score += SCORE_DATE_NEAR_SEASON
    return score


def resolve_subject(
    entry,
    *,
    opener,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleep=time.sleep,
    searcher=None,
) -> tuple[int | None, str]:
    """把一条档期条目解析成 Bangumi subject id。

    返回 ``(subject_id, 说明)``；解析不到时 id 为 None 并给出原因。
    先搜日文原名（Bangumi 主名是日文，命中率最高），不够再搜中文名。
    """
    search = searcher or search_subjects
    jp_raw = getattr(entry, "title_jp", None) or ""
    cn_raw = getattr(entry, "title_main", None) or getattr(entry, "title_cn", None) or ""
    jp = normalize_title(jp_raw)
    cn = normalize_title(cn_raw)
    window = season_window(getattr(entry, "season_code", "") or "")

    best_id: int | None = None
    best_score = 0
    best_name = ""

    for raw in (jp_raw, cn_raw):
        keyword = str(raw).strip()
        if not keyword:
            continue
        for candidate in search(
            keyword,
            opener=opener,
            base_url=base_url,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )[:10]:
            score = score_candidate(candidate, jp=jp, cn=cn, window=window)
            if score > best_score:
                best_score = score
                best_id = candidate.get("id")
                best_name = candidate.get("name") or ""
        if best_score >= MIN_SCORE:
            break  # 已经足够确信，不必再搜第二个关键词
        sleep(DEFAULT_SLEEP)

    if best_id and best_score >= MIN_SCORE:
        return int(best_id), f"命中「{best_name}」（置信 {best_score}）"
    if best_score:
        return None, f"置信不足（最高 {best_score} < {MIN_SCORE}）"
    return None, "未找到候选条目"


def fill_works_for_season(
    *,
    session_factory,
    season_code: str,
    apply: bool = False,
    base_url: str | None = None,
    opener=None,
    limit: int | None = MAX_ENTRIES,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleep=time.sleep,
    searcher=None,
    fetcher=None,
) -> FillSummary:
    """把某个档期里有、作品库里没有的作品补进作品库。

    - ``apply=False`` 只解析并统计「会补几条」，不写库；
    - 已在库（contents.bangumi_id 命中）的跳过，不重复拉取；
    - 单条失败不影响其余条目；连续搜索失败达到阈值则整轮中止。
    """
    summary = FillSummary(season_code=season_code)
    url = base_url or resolve_base_url()
    opener = opener or build_opener(None)
    search = searcher or search_subjects
    fetch = fetcher or fetch_subject
    cap = limit if limit and limit > 0 else None

    db = session_factory()
    try:
        entries = (
            db.query(AnimeSchedule)
            .filter(AnimeSchedule.season_code == season_code)
            .order_by(AnimeSchedule.id)
            .all()
        )
        summary.total = len(entries)
        if cap and len(entries) > cap:
            summary.capped = len(entries) - cap
            entries = entries[:cap]

        resolved: list[tuple[object, int, str]] = []
        consecutive_errors = 0
        for entry in entries:
            title = getattr(entry, "title_main", "") or getattr(entry, "title_cn", "") or "（未命名）"
            try:
                subject_id, reason = resolve_subject(
                    entry,
                    opener=opener,
                    base_url=url,
                    timeout=timeout,
                    retries=retries,
                    sleep=sleep,
                    searcher=search,
                )
                consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                consecutive_errors += 1
                summary.failed += 1
                summary.add_detail(title, "failed", str(exc)[:120])
                if consecutive_errors >= ABORT_AFTER_ERRORS:
                    summary.aborted = f"连续 {consecutive_errors} 次搜索失败：{exc}"[:200]
                    return summary
                continue

            if subject_id is None:
                summary.unresolved += 1
                summary.add_detail(title, "unresolved", reason)
                continue
            # 解析结果先记到档期行上：这是「将来收录自动连通」的锚点——
            # 之后这部作品无论何时入库，_map_and_store 都能反查到这里并补上
            # content_id（见 app.crud.link_schedules_by_subject_id）。
            if apply and entry.bangumi_subject_id != subject_id:
                entry.bangumi_subject_id = subject_id
                db.commit()
            resolved.append((entry, subject_id, reason))

        # 批量判重：一次查出这些 subject 里哪些已经在作品库。
        ids = [sid for _, sid, _ in resolved]
        existing: set[int] = set()
        if ids:
            rows = db.query(Content.bangumi_id).filter(Content.bangumi_id.in_(ids)).all()
            existing = {row[0] for row in rows if row[0] is not None}

        for entry, subject_id, reason in resolved:
            title = getattr(entry, "title_main", "") or getattr(entry, "title_cn", "") or "（未命名）"
            if subject_id in existing:
                summary.already += 1
                # 已在库：把档期条目关联到这部作品（档期卡片随即直跳详情页，
                # 档期译名并入作品 aliases，搜索用任一译名都能命中）。
                if apply:
                    content = crud.find_content_by_bangumi_id(db, subject_id)
                    if content is not None:
                        crud.link_schedule_to_content(
                            db,
                            entry,
                            content,
                            subject_id=subject_id,
                            alias_names=[
                                getattr(entry, "title_main", None),
                                getattr(entry, "title_cn", None),
                                getattr(entry, "title_jp", None),
                            ],
                        )
                continue
            if not apply:
                summary.pending += 1
                summary.add_detail(title, "pending", reason)
                continue
            try:
                payload = fetch(
                    subject_id,
                    opener=opener,
                    timeout=timeout,
                    retries=retries,
                    base_url=url,
                    sleep=sleep,
                )
                outcome = _map_and_store(
                    db,
                    payload,
                    apply=True,
                    cover_downloader=False,
                    summary_fallback=compose_fallback_summary(entry),
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                summary.failed += 1
                summary.add_detail(title, "failed", str(exc)[:120])
                continue

            if outcome.status == "imported":
                summary.imported += 1
                summary.add_detail(title, "imported", reason)
            elif outcome.status == "duplicate":
                summary.already += 1
            elif outcome.status == "skipped":
                summary.skipped += 1
                summary.add_detail(title, "skipped", outcome.reason[:120])
            else:
                summary.failed += 1
                summary.add_detail(title, outcome.status, outcome.reason[:120])
            sleep(DEFAULT_SLEEP)
    finally:
        db.close()

    return summary


def link_schedules_to_contents(
    *,
    session_factory,
    season_code: str | None = None,
    apply: bool = False,
    use_network: bool = True,
    limit: int | None = MAX_LINK_ENTRIES,
    base_url: str | None = None,
    opener=None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleep=time.sleep,
    searcher=None,
) -> LinkSummary:
    """把档期条目关联到作品库里已有的作品（不新建作品）。

    关联优先级（先用不联网的确定性手段，联网只是兜底）：

    1. **标题归一匹配**（不联网）：档期的日文原名 / 中文名 / 主标题
       归一后比对作品的 title_original / title / aliases。命中即同时
       拿到 content_id 与 bangumi_id——这正是「翻译不一样也能对上」的关键：
       日文原名是跨语种稳定的锚点。
    2. **Bangumi 解析兜底**（联网）：标题匹配不上时，用镜像站把档期标题
       解析成 Bangumi subject_id，再按 contents.bangumi_id 精确配对；
       作品库里还没有这部作品时，把 subject_id 记到档期行上，将来收录
       之后即可自动连通（见 app.crud.link_schedules_by_subject_id）。

    同时把档期的各语种译名并入作品 aliases，让全站搜索用任一译名都能
    搜到这部作品（档期页点击即命中详情页）。

    - ``apply=False`` 只统计不写库；
    - ``season_code`` 为空时处理全部档期条目。
    """
    summary = LinkSummary()
    url = base_url or resolve_base_url()
    opener = opener or build_opener(None)
    search = searcher or search_subjects
    cap = limit if limit and limit > 0 else None

    db = session_factory()
    try:
        stmt = db.query(AnimeSchedule).order_by(AnimeSchedule.id)
        if season_code:
            stmt = stmt.filter(AnimeSchedule.season_code == season_code)
        entries = list(stmt.all())
        summary.total = len(entries)
        if cap and len(entries) > cap:
            summary.skipped = len(entries) - cap
            entries = entries[:cap]

        # 标题索引一次建好，全程复用（避免逐条查库）。
        index = crud.build_schedule_title_index(db)
        consecutive_errors = 0

        for entry in entries:
            title = (
                getattr(entry, "title_main", "")
                or getattr(entry, "title_cn", "")
                or getattr(entry, "title_jp", "")
                or "（未命名）"
            )
            # 已经关联好、且指向的作品仍在库：无需处理。
            if entry.content_id:
                linked_content = db.get(Content, entry.content_id)
                if linked_content is not None:
                    summary.already += 1
                    continue

            content = crud.match_content_for_schedule(db, entry, index)
            subject_id: int | None = None
            reason = "标题归一匹配"
            if content is None:
                if not use_network:
                    summary.unmatched += 1
                    summary.add_detail(title, "unmatched", "标题未匹配且未启用联网解析")
                    continue
                try:
                    subject_id, resolve_reason = resolve_subject(
                        entry,
                        opener=opener,
                        base_url=url,
                        timeout=timeout,
                        retries=retries,
                        sleep=sleep,
                        searcher=search,
                    )
                    consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                    consecutive_errors += 1
                    summary.failed += 1
                    summary.add_detail(title, "failed", str(exc)[:120])
                    if consecutive_errors >= ABORT_AFTER_ERRORS:
                        summary.aborted = f"连续 {consecutive_errors} 次搜索失败：{exc}"[:200]
                        return summary
                    continue
                if subject_id is None:
                    summary.unmatched += 1
                    summary.add_detail(title, "unmatched", resolve_reason)
                    continue
                reason = resolve_reason
                content = crud.find_content_by_bangumi_id(db, subject_id)
                if content is None:
                    # 作品库里还没有：把 subject_id 记到档期行，将来收录即自动连通。
                    if apply:
                        entry.bangumi_subject_id = subject_id
                        db.commit()
                    summary.unmatched += 1
                    summary.add_detail(title, "unmatched", f"已解析但作品库暂无：{reason}")
                    continue

            if apply:
                crud.link_schedule_to_content(
                    db,
                    entry,
                    content,
                    subject_id=subject_id if subject_id is not None else content.bangumi_id,
                    alias_names=[entry.title_main, entry.title_cn, entry.title_jp],
                )
            summary.linked += 1
            summary.add_detail(title, "linked", f"{reason} → contents.id={content.id}")
    finally:
        db.close()

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把新番档期里有、作品库里没有的作品补进作品库",
    )
    parser.add_argument(
        "--season",
        default=None,
        metavar="YYYYMM",
        help="档期码（补库模式必填；关联模式可用 --all-seasons 处理全部）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="正式入库（不加则只预览，不写库）",
    )
    parser.add_argument(
        "--link-only",
        action="store_true",
        help="只做「档期 ↔ 作品库」关联回填，不新建作品",
    )
    parser.add_argument(
        "--all-seasons",
        action="store_true",
        help="配合 --link-only：不限定档期码，处理全部档期条目",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="配合 --link-only：只用标题归一匹配，不调用 Bangumi 解析",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="数据库连接串（默认取配置 / .env）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ENTRIES,
        help=f"单次最多处理条目数（默认 {MAX_ENTRIES}，关联模式用 {MAX_LINK_ENTRIES}）",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from app.config import settings
    from app.database import build_engine
    from sqlalchemy.orm import sessionmaker

    engine = build_engine(args.database_url or settings.database_url, busy_timeout=60)
    # 档期↔作品库关联列（无 Alembic，幂等补列）：老库先补列再读写。
    crud.ensure_schedule_link_columns(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    if args.link_only:
        season = None if args.all_seasons else (args.season or "").strip() or None
        print(f"档期：{season or '全部'}")
        print(f"模式：{'正式关联' if args.apply else '预览（不写库）'}")
        print(f"联网解析：{'关闭' if args.no_network else '开启'}")
        summary = link_schedules_to_contents(
            session_factory=session_factory,
            season_code=season,
            apply=args.apply,
            use_network=not args.no_network,
            limit=MAX_LINK_ENTRIES if args.limit == MAX_ENTRIES else args.limit,
        )
        print(summary.message)
        for detail in summary.details[:20]:
            print(f"  - [{detail['status']}] {detail['title']}：{detail['reason']}")
        return 0

    if not (args.season or "").strip():
        print("用法错误：补库模式必须指定 --season（或用 --link-only 做关联回填）")
        return 2

    print(f"档期：{args.season}")
    print(f"模式：{'正式入库' if args.apply else '预览（不写库）'}")
    print(f"接口：{resolve_base_url()}")
    summary = fill_works_for_season(
        session_factory=session_factory,
        season_code=args.season.strip(),
        apply=args.apply,
        limit=args.limit,
    )
    print(summary.message)
    for detail in summary.details[:20]:
        print(f"  - [{detail['status']}] {detail['title']}：{detail['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
