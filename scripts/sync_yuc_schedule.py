"""Yuc.wiki 新番档期同步脚本：Atom 发现档期 -> 抓取档期页 -> 解析作品 -> 写库。

完整流程：Atom 读取 → 档期发现 → HTML 请求 → 作品解析 → 字段清洗 →
去重和更新 → 数据库同步 → 结果汇总。

设计要点（给后续维护者）：
- 默认 dry-run：只预览统计，不写任何新番数据；显式 --apply 才正式入库。
  两种模式都会保存一条同步运行记录（schedule_sync_runs），便于审计。
- 代理可配置：--proxy 参数或 YUC_PROXY 环境变量；默认直连，不读系统代理。
- 只用 Python 标准库（urllib / html.parser / xml.etree），不新增依赖。
- 幂等：按 (season_code, 规范化主标题) 去重；重复同步只新增/更新/标记不变，
  不删除本地已有记录，也不触碰 Content、Article 等其他模块。
- 档期识别依据 Atom 条目链接中的 /YYYYMM/ 路径（如 /202610/），
  按“年-月”数值比较选最新，不依赖条目更新时间。
- 只保存经确认的图片 URL 和播放平台链接，不下载文件、不猜测字段。

用法示例：
    # 列出 Atom 中发现的全部档期
    python scripts/sync_yuc_schedule.py --list-seasons
    # 预览最新档期（默认，不写库）
    python scripts/sync_yuc_schedule.py
    # 预览指定档期
    python scripts/sync_yuc_schedule.py --season 202610
    # 正式同步最新档期
    python scripts/sync_yuc_schedule.py --apply
    # 走本机代理
    python scripts/sync_yuc_schedule.py --proxy http://127.0.0.1:9910

本文件同时是可复用模块：parse_atom / split_entries / parse_entry_chunk /
run_sync 等函数可被测试直接调用，见 tests/test_yuc_schedule.py。
"""

from __future__ import annotations

import argparse
import html.parser
import http.client
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path

# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402

# ---------- 常量与配置 ----------

YUC_BASE_URL = "https://yuc.wiki"
ATOM_URL = f"{YUC_BASE_URL}/atom.xml"
DEFAULT_USER_AGENT = "acgn-test-platform/0.1 (anime schedule sync; local project)"
DEFAULT_TIMEOUT = 15.0  # 单次请求超时（秒）
DEFAULT_RETRIES = 2  # 失败后的重试次数（总请求次数 = 1 + retries）
DEFAULT_DELAY = 0.5  # 两次真实网络请求之间的间隔（秒），避免高频请求

RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# 档期页链接形如 /202610/（2026 年 10 月新番表）；月份限定 01-12。
SEASON_PATH_RE = re.compile(r"/(20\d{2})(0[1-9]|1[0-2])(?:/|$)")
# 档期页详情条目以 HTML 注释 <!--#A01--> 等分块（站点模板约定）。
ENTRY_MARKER_RE = re.compile(r"<!--\s*#\s*[A-Za-z]+\d+\s*-->")
TITLE_MAIN_ANCHOR_RE = re.compile(r"class=[\"']title_main_r[\"']")
POSTER_IMG_RE = re.compile(r"<img[^>]*>", re.I)
DATA_SRC_RE = re.compile(r"""data-src=["']([^"']+)["']""", re.I)

# 解析为星期几（1=周一 … 7=周日）的文本模式：周X / 星期X / X曜。
WEEKDAY_CHAR = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])|([一二三四五六日天])曜")
BROADCAST_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")

# 只有命中这些已知视频平台域名的链接才认定为“播放平台”；
# 官网、PV 等其他链接不存储，避免把宣传页误当平台。
PLATFORM_DOMAINS = (
    "bilibili.com",
    "iqiyi.com",
    "iq.com",
    "youku.com",
    "v.qq.com",
    "netflix.com",
    "primevideo.com",
    "disneyplus.com",
    "crunchyroll.com",
    "hulu.jp",
    "hulu.com",
    "abema.tv",
    "d-anime.store",
    "animestore.docomo.ne.jp",
    "nicovideo.jp",
    "u-next.com",
    "b-ch.com",
    "musechina.com",
    "gamer.com.tw",
)
# 锚文本为这些时视为宣传视频（PV/CM），即使域名是平台也不作为平台链接。
PROMO_ANCHOR_TEXTS = {"PV", "CM", "预告", "預告", "PV1", "PV2", "PV映像"}

TEXT_MAX_LEN = 200  # 与模型 String(200) 字段一致


class YucFetchError(RuntimeError):
    """访问 Yuc.wiki 失败（网络错误、超时、HTTP 错误状态、解析失败等）。"""


class SeasonNotFoundError(ValueError):
    """指定的档期在 Atom 中不存在；携带可用档期列表。"""

    def __init__(self, season_code: str, available: list["SeasonInfo"]):
        codes = "、".join(s.code for s in available) or "（无）"
        super().__init__(f"档期 {season_code} 不存在。可用档期：{codes}")
        self.season_code = season_code
        self.available = available


# ---------- 数据结构 ----------


@dataclass
class SeasonInfo:
    """Atom 中发现的一个档期。"""

    code: str  # 档期标识，如 202610
    title: str  # Atom 条目标题，如 “2026年10月新番表”
    url: str  # 档期页地址


@dataclass
class ParsedEntry:
    """从单个 HTML 条目块解析出的字段（未清洗为入库 dict 前的形态）。"""

    title_cn: str | None = None
    title_jp: str | None = None
    weekday: int | None = None
    broadcast_time: str | None = None
    broadcast_note: str | None = None
    image_url: str | None = None
    platforms: list[dict] = field(default_factory=list)

    @property
    def title_main(self) -> str | None:
        """主标题：优先中文标题，缺失时回退日文标题。"""
        return self.title_cn or self.title_jp


@dataclass
class EntryOutcome:
    """单个条目的处理结果，用于输出跳过/失败原因。"""

    title: str
    status: str  # added / updated / unchanged / duplicate / skipped / failed / preview
    reason: str = ""


@dataclass
class SyncSummary:
    """一次同步运行的汇总统计与结果明细。"""

    mode: str  # dry-run / apply
    season_code: str
    season_title: str
    season_url: str
    pages_fetched: int = 0
    parsed: int = 0
    valid: int = 0
    preview: int = 0  # dry-run 下可入库的条数
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    outcomes: list[EntryOutcome] = field(default_factory=list)
    run_id: int | None = None

    @property
    def issues(self) -> list[EntryOutcome]:
        """需要向用户说明原因的结果（跳过/失败/重复）。"""
        return [o for o in self.outcomes if o.status in {"skipped", "failed", "duplicate"}]

    def describe(self) -> str:
        mode_label = "正式同步（--apply）" if self.mode == "apply" else "预览（dry-run）"
        lines = [
            f"档期：{self.season_code} {self.season_title}（{self.season_url}）",
            f"模式：{mode_label}",
            "同步汇总：",
            f"  请求页面：{self.pages_fetched}",
            f"  解析条目：{self.parsed}",
            f"  有效记录：{self.valid}",
            f"  可入库：{self.valid}"
            + ("（dry-run 未写入，仅预览）" if self.mode != "apply" else ""),
            f"  新增：{self.added}  更新：{self.updated}  不变：{self.unchanged}",
            f"  重复：{self.duplicates}  跳过：{self.skipped}  失败：{self.failed}",
        ]
        return "\n".join(lines)


# ---------- 文本清洗 ----------


def clean_text(text: str | None) -> str | None:
    """去首尾空白、折叠连续空白（含全角空格）为单个空格；空串返回 None。"""
    if text is None:
        return None
    collapsed = re.sub(r"[\s\u3000]+", " ", text).strip()
    return collapsed or None


def limit_text(text: str | None, max_len: int = TEXT_MAX_LEN) -> str | None:
    """超长字段截断到列宽（标题/说明类字段超长时保显示不阻断同步）。"""
    if text is None:
        return None
    return text[:max_len]


def normalize_title_key(title: str | None) -> str | None:
    """规范化主标题作为源站内唯一键：去全部空白、转小写。"""
    if not title:
        return None
    return re.sub(r"[\s\u3000]+", "", title).lower() or None


def extract_weekday(text: str | None) -> int | None:
    """从播出说明中提取星期几（1=周一 … 7=周日）；识别不了返回 None。"""
    if not text:
        return None
    match = WEEKDAY_RE.search(text)
    if not match:
        return None
    char = match.group(1) or match.group(2)
    return WEEKDAY_CHAR.get(char)


def extract_broadcast_time(text: str | None) -> str | None:
    """从播出说明中提取 HH:MM 形式的播出时间（含 24:00+ 深夜时段）。"""
    if not text:
        return None
    match = BROADCAST_TIME_RE.search(text)
    return match.group(1) if match else None


def platform_from_link(text: str | None, href: str) -> dict | None:
    """判断一个 <a> 链接是否为可确认的播放平台；是则返回 {name, url}。

    规则（保守，避免误判）：
    - 必须是 http(s) 绝对链接；
    - 域名命中已知视频平台域名列表；
    - 锚文本为 PV/CM 等宣传视频时不算平台。
    """
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in PLATFORM_DOMAINS):
        return None
    label = clean_text(text)
    if label and label.upper() in PROMO_ANCHOR_TEXTS:
        return None
    return {"name": label or host, "url": href}


# ---------- Atom 解析与档期发现 ----------


def parse_atom(atom_text: str) -> list[SeasonInfo]:
    """解析 Atom 订阅源，发现全部档期条目，按档期年月新到旧排序。

    档期识别依据条目链接中的 /YYYYMM/ 路径；其余条目（观测站、剧场版等）跳过。
    """
    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError as exc:
        raise YucFetchError(f"Atom XML 解析失败：{exc}") from exc

    seasons: dict[str, SeasonInfo] = {}
    for entry in root.findall(f"{ATOM_NS}entry"):
        link = None
        for candidate in entry.findall(f"{ATOM_NS}link"):
            href = (candidate.get("href") or "").strip()
            if SEASON_PATH_RE.search(href):
                link = href
                break
        if link is None:
            continue
        match = SEASON_PATH_RE.search(link)
        code = match.group(1) + match.group(2)
        title_el = entry.find(f"{ATOM_NS}title")
        title = unescape(title_el.text or "").strip() if title_el is not None else ""
        seasons.setdefault(code, SeasonInfo(code=code, title=title, url=link))

    return sorted(
        seasons.values(),
        key=lambda s: (int(s.code[:4]), int(s.code[4:])),
        reverse=True,
    )


def pick_season(seasons: list[SeasonInfo], season_code: str | None) -> SeasonInfo:
    """选择目标档期：显式指定时必须存在（否则抛 SeasonNotFoundError），
    未指定时取最新档期（按年月比较，不按 Atom 更新时间）。"""
    if season_code is None:
        if not seasons:
            raise YucFetchError("Atom 中未发现任何档期页面")
        return seasons[0]
    for season in seasons:
        if season.code == season_code:
            return season
    raise SeasonNotFoundError(season_code, seasons)


# ---------- HTML 条目切分与解析 ----------


@dataclass
class EntryChunk:
    """切分出的单个条目块及其海报获取方式。"""

    html: str
    fallback_image: str | None = None
    # 标记分块：海报 <img> 在块内，可信；回退分块：海报只从紧邻前文提取，
    # 块尾可能带上下一条目的海报，因此不信任块内图片扫描结果。
    trust_embedded_image: bool = True


def split_entries(html_text: str) -> list[EntryChunk]:
    """把档期页 HTML 切成单个作品的条目块。

    首选：按站点模板的 <!--#A01--> 类注释标记分块，海报图在块内。
    回退：无标记时按 title_main_r 出现位置分块，海报取该位置前最近的
    data-src 图片（两者之间只隔着当前条目的海报 div，见站点结构）。
    """
    markers = list(ENTRY_MARKER_RE.finditer(html_text))
    if markers:
        chunks = []
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(html_text)
            chunks.append(EntryChunk(html=html_text[marker.end():end]))
        return chunks

    anchors = list(TITLE_MAIN_ANCHOR_RE.finditer(html_text))
    chunks = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(html_text)
        # 海报 div 紧贴在 title_main_r 之前（中间只隔表格开头），
        # 回看窗口过大会误取到上一条目的海报，因此限制在 600 字符内。
        lookback = html_text[max(0, anchor.start() - 600):anchor.start()]
        poster = None
        for img_tag in reversed(POSTER_IMG_RE.findall(lookback)):
            src = DATA_SRC_RE.search(img_tag)
            if src:
                poster = src.group(1)
                break
        chunks.append(
            EntryChunk(
                html=html_text[anchor.start():end],
                fallback_image=poster,
                trust_embedded_image=False,
            )
        )
    return chunks


class _EntryParser(html.parser.HTMLParser):
    """单条目块的字段提取器（标准库 HTMLParser，状态机式收集）。

    收集目标：
    - p.title_cn_r / p.title_jp_r / p.broadcast_r / p.broadcast_ex_r 的文本；
    - td.link_a_r / td.link_b_r 内的全部 <a> 链接（文本 + href）；
    - 块内第一个 width 含 180 的 <img> 的 data-src（海报）。
    """

    CAPTURE_CLASSES = {"title_cn_r", "title_jp_r", "broadcast_r", "broadcast_ex_r"}
    LINK_CELL_CLASSES = {"link_a_r", "link_b_r"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: dict[str, list[str]] = {name: [] for name in self.CAPTURE_CLASSES}
        self.links: list[tuple[str, str]] = []  # (锚文本, href)
        self.poster: str | None = None
        self._capture: str | None = None  # 正在收集的 p 类名
        self._capture_depth = 0
        self._buf: list[str] = []
        self._in_link_cell = False
        self._anchor: list | None = None  # [href, 文本缓冲]

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes = attr_dict.get("class") or ""

        if tag == "img":
            if self.poster is None:
                src = attr_dict.get("data-src") or attr_dict.get("src")
                width = attr_dict.get("width") or ""
                if src and "180" in width:
                    self.poster = src.strip()
            return

        if self._capture is not None:
            if tag == "p":
                self._capture_depth += 1
            return

        if tag == "p" and classes in self.CAPTURE_CLASSES:
            self._capture = classes
            self._capture_depth = 1
            self._buf = []
        elif tag == "td" and classes in self.LINK_CELL_CLASSES:
            self._in_link_cell = True
        elif tag == "a" and self._in_link_cell:
            href = (attr_dict.get("href") or "").strip()
            if href:
                self._anchor = [href, []]

    def handle_endtag(self, tag):
        if self._anchor is not None and tag == "a":
            self.links.append((clean_text("".join(self._anchor[1])) or "", self._anchor[0]))
            self._anchor = None
        if self._capture is not None and tag == "p":
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                self.texts[self._capture].append("".join(self._buf))
                self._capture = None
        if tag == "td" and self._in_link_cell:
            self._in_link_cell = False

    def handle_data(self, data):
        if self._capture is not None:
            self._buf.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def finalize(self):
        """解析结束时调用：把未正常闭合的捕获缓冲落袋（容忍畸形 HTML）。"""
        if self._capture is not None:
            self.texts[self._capture].append("".join(self._buf))
            self._capture = None
        if self._anchor is not None:
            self.links.append((clean_text("".join(self._anchor[1])) or "", self._anchor[0]))
            self._anchor = None


def parse_entry_chunk(
    chunk_html: str,
    fallback_image: str | None = None,
    *,
    allow_embedded_image: bool = True,
) -> ParsedEntry:
    """解析单个条目块为 ParsedEntry；字段缺失时保留为 None，不猜测。

    allow_embedded_image=False 时忽略块内 <img>（回退切块的块尾可能带着
    下一条目的海报），只使用 fallback_image。
    """
    parser = _EntryParser()
    try:
        parser.feed(chunk_html)
        parser.close()
    except Exception as exc:  # html.parser 对畸形输入宽容，这里兜底防御
        raise YucFetchError(f"条目 HTML 解析异常：{exc}") from exc
    parser.finalize()

    title_cn = clean_text(parser.texts["title_cn_r"][0]) if parser.texts["title_cn_r"] else None
    title_jp = clean_text(parser.texts["title_jp_r"][0]) if parser.texts["title_jp_r"] else None

    broadcast_parts = []
    if parser.texts["broadcast_r"]:
        part = clean_text(parser.texts["broadcast_r"][0])
        if part:
            broadcast_parts.append(part)
    if parser.texts["broadcast_ex_r"]:
        part = clean_text(parser.texts["broadcast_ex_r"][0])
        if part:
            broadcast_parts.append(part)
    broadcast_note = "；".join(broadcast_parts) or None

    platforms: list[dict] = []
    seen_urls: set[str] = set()
    for text, href in parser.links:
        platform = platform_from_link(text, href)
        if platform and platform["url"] not in seen_urls:
            platforms.append(platform)
            seen_urls.add(platform["url"])

    image = (parser.poster if allow_embedded_image else None) or fallback_image
    return ParsedEntry(
        title_cn=title_cn,
        title_jp=title_jp,
        weekday=extract_weekday(broadcast_note),
        broadcast_time=extract_broadcast_time(broadcast_note),
        broadcast_note=limit_text(broadcast_note),
        image_url=limit_text(image, 500),
        platforms=platforms,
    )


# ---------- 网络请求 ----------


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    """按代理配置构建 opener：配置代理则走代理，否则显式直连（不读系统代理）。"""
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def fetch_url(
    url: str,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep=time.sleep,
) -> str:
    """抓取 URL 文本内容：有限重试、明确错误提示；全部尝试失败抛 YucFetchError。"""
    last_reason = "未知错误"
    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 5))
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept": "text/html, application/xml"}
            )
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                continue
            raise YucFetchError(f"请求 {url} 失败：{last_reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_reason = "请求超时" if isinstance(exc, TimeoutError) else f"网络错误：{exc}"
            if attempt < retries:
                continue
            raise YucFetchError(f"请求 {url} 失败：{last_reason}") from exc
    raise YucFetchError(f"请求 {url} 失败：{last_reason}")


def _make_fetcher(*, opener, timeout, retries, user_agent, sleep=time.sleep):
    """把 fetch_url 的固定参数绑定为 fetch(url) -> str 的简单函数。"""

    def fetch(url: str) -> str:
        return fetch_url(
            url, opener=opener, timeout=timeout, retries=retries,
            user_agent=user_agent, sleep=sleep,
        )

    return fetch


# ---------- 同步主流程 ----------


def run_sync(
    season_code: str | None,
    *,
    session_factory,
    apply: bool = False,
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    delay: float = DEFAULT_DELAY,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = YUC_BASE_URL,
    fetch=None,
    sleep=time.sleep,
) -> SyncSummary:
    """同步一个档期：Atom 发现 -> 抓取档期页 -> 解析 -> 去重 -> 写库 -> 汇总。

    - fetch：可选的 fetch(url) -> str 注入点，测试用它替代真实网络；
      不传时按 proxy/timeout 等参数构造真实请求器（请求间加 delay 限频）。
    - dry-run 不写新番数据；两种模式都保存同步运行记录。
    - 单条写入失败不中断整体；同步不删除本地已有记录。
    """
    mode = "apply" if apply else "dry-run"
    if fetch is None:
        fetch = _make_fetcher(
            opener=build_opener(proxy), timeout=timeout, retries=retries,
            user_agent=user_agent, sleep=sleep,
        )

    atom_text = fetch(f"{base_url.rstrip('/')}/atom.xml")
    seasons = parse_atom(atom_text)
    if not seasons:
        raise YucFetchError(f"Atom 中未发现任何档期页面（{base_url}/atom.xml）")
    season = pick_season(seasons, season_code)  # 不存在时抛 SeasonNotFoundError

    summary = SyncSummary(
        mode=mode, season_code=season.code, season_title=season.title, season_url=season.url
    )
    db = session_factory()
    try:
        run = crud.create_sync_run(db, season_code=season.code, mode=mode)
        summary.run_id = run.id

        if fetch is None and delay > 0:  # 仅真实网络请求时限频
            sleep(delay)
        try:
            page_html = fetch(season.url)
        except YucFetchError as exc:
            summary.failed = 1
            summary.pages_fetched = 0
            crud.finish_sync_run(
                db, run, summary={"pages_fetched": 0, "failed": 1,
                                  "message": f"档期页请求失败：{exc}"}
            )
            summary.outcomes.append(EntryOutcome(title="", status="failed", reason=str(exc)))
            return summary
        summary.pages_fetched = 1

        issues: list[str] = []
        seen_keys: set[str] = set()
        for chunk in split_entries(page_html):
            summary.parsed += 1
            try:
                entry = parse_entry_chunk(
                    chunk.html,
                    fallback_image=chunk.fallback_image,
                    allow_embedded_image=chunk.trust_embedded_image,
                )
            except YucFetchError as exc:
                summary.parsed -= 1
                summary.skipped += 1
                reason = f"HTML 解析失败：{exc}"
                issues.append(reason)
                summary.outcomes.append(EntryOutcome(title="", status="skipped", reason=reason))
                continue

            title = entry.title_main
            if not title:
                summary.skipped += 1
                reason = "缺少主标题（title_cn 与 title_jp 均为空）"
                issues.append(reason)
                summary.outcomes.append(EntryOutcome(title="", status="skipped", reason=reason))
                continue

            key = normalize_title_key(title)
            if not key:
                summary.skipped += 1
                reason = f"主标题无法规范化：{title!r}"
                issues.append(reason)
                summary.outcomes.append(EntryOutcome(title=title, status="skipped", reason=reason))
                continue
            if key in seen_keys:
                summary.duplicates += 1
                summary.outcomes.append(
                    EntryOutcome(title=title, status="duplicate", reason="同档期内重复作品，仅保留首条")
                )
                continue
            seen_keys.add(key)
            summary.valid += 1

            data = {
                "season_code": season.code,
                "source_key": limit_text(key),
                "title_main": limit_text(title),
                "title_cn": limit_text(entry.title_cn),
                "title_jp": limit_text(entry.title_jp),
                "weekday": entry.weekday,
                "broadcast_time": limit_text(entry.broadcast_time, 20),
                "broadcast_note": limit_text(entry.broadcast_note),
                "image_url": entry.image_url,
                "platforms": entry.platforms,
                "source_url": season.url,
            }

            if not apply:
                summary.preview += 1
                summary.outcomes.append(EntryOutcome(title=title, status="preview"))
                continue

            try:
                _, action = crud.upsert_schedule(db, data)
            except Exception as exc:  # 单条数据库错误不中断整体同步
                db.rollback()
                summary.failed += 1
                reason = f"数据库写入失败：{exc}"
                issues.append(f"{title}：{reason}")
                summary.outcomes.append(EntryOutcome(title=title, status="failed", reason=reason))
                continue

            if action == "added":
                summary.added += 1
            elif action == "updated":
                summary.updated += 1
            else:
                summary.unchanged += 1
            summary.outcomes.append(EntryOutcome(title=title, status=action))

        message = "dry-run 预览：未写入任何新番数据" if not apply else ""
        if issues:
            message = "；".join(part for part in [message, *issues[:10]] if part)
            if len(issues) > 10:
                message += f"；（其余 {len(issues) - 10} 条问题省略）"
        crud.finish_sync_run(
            db,
            run,
            summary={
                "pages_fetched": summary.pages_fetched,
                "parsed": summary.parsed,
                "valid": summary.valid,
                "added": summary.added,
                "updated": summary.updated,
                "unchanged": summary.unchanged,
                "duplicates": summary.duplicates,
                "skipped": summary.skipped,
                "failed": summary.failed,
                "message": message or None,
            },
        )
        return summary
    finally:
        db.close()


# ---------- 命令行入口 ----------


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数定义（独立成函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description=(
            "从 Yuc.wiki 同步新番档期到本站 anime_schedules 表"
            "（默认 dry-run 预览，--apply 才写入）。"
        ),
    )
    parser.add_argument(
        "--season",
        default=None,
        metavar="YYYYMM",
        help="指定档期（如 202610）；缺省时自动选择 Atom 中最新的档期",
    )
    parser.add_argument(
        "--list-seasons",
        action="store_true",
        help="只列出 Atom 中发现的全部可用档期，不做同步",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="正式写入数据库；不加此参数则只预览，不写新番数据",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="HTTP(S) 代理地址，如 http://127.0.0.1:9910；也可用环境变量 YUC_PROXY；默认直连",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES,
        help=f"失败重试次数（默认 {DEFAULT_RETRIES}）",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=f"两次真实网络请求的最小间隔秒数（默认 {DEFAULT_DELAY:g}）",
    )
    parser.add_argument(
        "--user-agent", default=DEFAULT_USER_AGENT, help="请求使用的 User-Agent"
    )
    parser.add_argument(
        "--database-url", default=None,
        help="数据库连接 URL；默认读取应用配置 DATABASE_URL（本地默认 SQLite）",
    )
    return parser


def resolve_proxy(explicit_proxy: str | None, env=None) -> str | None:
    """代理取值优先级：--proxy 参数 > YUC_PROXY 环境变量 > 无（直连）。"""
    if env is None:
        env = os.environ
    return explicit_proxy or env.get("YUC_PROXY") or None


def print_seasons(seasons: list[SeasonInfo]) -> None:
    """输出可用档期列表。"""
    print(f"共发现 {len(seasons)} 个档期（新到旧）：")
    for season in seasons:
        print(f"  {season.code}  {season.title or '（无标题）'}  {season.url}")


def main(argv=None) -> int:
    """CLI 入口；退出码：0 成功，1 同步失败，2 档期不存在或参数无效。"""
    args = build_parser().parse_args(argv)
    proxy = resolve_proxy(args.proxy)

    if args.season is not None and not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", args.season):
        print(f"错误：--season 需为 YYYYMM 形式的合法年月（收到 {args.season!r}）")
        return 2

    fetch = _make_fetcher(
        opener=build_opener(proxy), timeout=args.timeout, retries=args.retries,
        user_agent=args.user_agent,
    )

    print(f"代理：{proxy if proxy else '未启用（直连）'}")
    try:
        seasons = parse_atom(fetch(ATOM_URL))
    except YucFetchError as exc:
        print(f"错误：{exc}")
        return 1

    if args.list_seasons:
        print_seasons(seasons)
        return 0
    if not seasons:
        print("错误：Atom 中未发现任何档期页面，无法同步")
        return 1

    database_url = args.database_url or settings.database_url
    print(f"数据库：{database_url}")
    engine = build_engine(database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        try:
            summary = run_sync(
                args.season,
                session_factory=session_factory,
                apply=args.apply,
                proxy=proxy,
                timeout=args.timeout,
                retries=args.retries,
                delay=args.delay,
                user_agent=args.user_agent,
                fetch=fetch,
            )
        except SeasonNotFoundError as exc:
            print(f"错误：{exc}")
            print_seasons(exc.available)
            return 2
    finally:
        engine.dispose()

    for outcome in summary.issues:
        label = {"duplicate": "重复", "skipped": "跳过", "failed": "失败"}[outcome.status]
        parts = [f"[{label}]"]
        if outcome.title:
            parts.append(f"《{outcome.title}》")
        if outcome.reason:
            parts.append(f"- {outcome.reason}")
        print(" ".join(parts))
    print(summary.describe())
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
