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
- 站点会给同类名追加数字序号区分样式变体（title_cn_r1/r2、title_jp_r3、
  staff_r2 等），类名匹配前统一去掉尾部数字，见 normalize_class_token。
- 具体播出时间（HH:MM）不在条目里，而在页面下方星期区播出位
  （div_date 块的 imgtext4/imgtext5，如 "24:35~"）；按海报 URL 关联、
  标题规范化匹配兜底回填到 broadcast_time；关联不上保持 NULL，不猜测。
- 播放平台链接同理也可能只在星期区 tr_area（分区域平台，如“港台”）：
  按同一关联合并进 platforms，条目自带平台优先，域名仍走白名单。
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
    # 批量同步最近 3 个档期（回填历史档期，按新到旧顺序）
    python scripts/sync_yuc_schedule.py --seasons 3 --apply
    # 源站 TLS 证书过期时跳过校验（2026-10 起 yuc.wiki 需要）
    python scripts/sync_yuc_schedule.py --seasons 3 --apply --insecure
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
import ssl
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
# 站点对脚本型 UA 返回 403（2026-10 核对），必须用浏览器 UA 才能取到列表/档期页。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15.0  # 单次请求超时（秒）
DEFAULT_RETRIES = 2  # 失败后的重试次数（总请求次数 = 1 + retries）
DEFAULT_DELAY = 0.5  # 两次真实网络请求之间的间隔（秒），避免高频请求

RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# 档期页链接形如 /202610/（2026 年 10 月新番表）；月份限定 01-12。
SEASON_PATH_RE = re.compile(r"/(20\d{2})(0[1-9]|1[0-2])(?:/|$)")

# 特殊档期分类：源站 Atom 条目标题/链接 → 本站分类码与展示名。
# 站点原文（新番卫星观测站 / Movie-OVA-SP / 内地上映动画电影）只在抓取时用作
# 匹配键，入库即改写为本站点名；展示名与 app.crud.SCHEDULE_CATEGORY_LABELS 同源。
# - path：Atom 条目链接里的路径片段（最稳，标题常被站点改写），命中即认定；
# - title：标题包含的子串，path 缺失或拿不到链接时兜底（如内地上映动画电影）。
SPECIAL_CATEGORIES = (
    {"code": "unreleased", "label": "未上线新番", "path": "/new/"},
    {"code": "ova_sp", "label": "剧场版ova与特别篇", "path": "/sp/"},
    {"code": "cn_movie", "label": "大陆引进剧场版", "title": "内地上映动画电影"},
)
# 档期页详情条目以 HTML 注释 <!--#A01--> 等分块（站点模板约定）。
ENTRY_MARKER_RE = re.compile(r"<!--\s*#\s*[A-Za-z]+\d+\s*-->")
TITLE_MAIN_ANCHOR_RE = re.compile(r"class=[\"']title_main(?:_r\d*)?[\"']")
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
    work_type: str | None = None  # 作品类型，如 漫画改编动画 / 原创动画
    work_tags: str | None = None  # 题材标签，站点原文（斜杠/换行分隔）

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
    weekday_slots: int = 0  # 页面星期区发现的播出位数量（具体时间来源）
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
            f"  星期区播出位：{self.weekday_slots}",
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
    # 锚文本是「[独家]」这类装饰标记时不算有效名称：回退用域名命名
    if label and re.fullmatch(r"[\[\(【（].+[\]\)】）]", label):
        label = ""
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


def parse_special_categories(atom_text: str) -> list[SeasonInfo]:
    """从 Atom 中发现三个特殊档期分类：未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版。

    匹配规则（见 SPECIAL_CATEGORIES）：优先按条目链接路径片段（/new/、/sp/），
    拿不到链接或路径未知时按标题子串兜底（内地上映动画电影）。命中后把站点原文
    改写为本站展示名作为 title；返回顺序与 SPECIAL_CATEGORIES 一致，未出现的分类略去。
    """
    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError as exc:
        raise YucFetchError(f"Atom XML 解析失败：{exc}") from exc

    found: dict[str, SeasonInfo] = {}
    for entry in root.findall(f"{ATOM_NS}entry"):
        link_el = entry.find(f"{ATOM_NS}link")
        link = (link_el.get("href") or "").strip() if link_el is not None else ""
        title_el = entry.find(f"{ATOM_NS}title")
        title = unescape(title_el.text or "").strip() if title_el is not None else ""
        for spec in SPECIAL_CATEGORIES:
            if spec.get("path") and spec["path"] in link:
                found[spec["code"]] = SeasonInfo(code=spec["code"], title=spec["label"], url=link)
                break
            if spec.get("title") and spec["title"] in title:
                found[spec["code"]] = SeasonInfo(code=spec["code"], title=spec["label"], url=link)
                break
    return [found[c["code"]] for c in SPECIAL_CATEGORIES if c["code"] in found]


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

# 站点会给同类名追加数字序号区分样式变体（title_cn_r1、title_jp_r2、staff_r1…），
# 精确匹配会漏掉带序号的作品；匹配前统一去掉类名尾部数字，向后兼容 r4/r5 等新变体。
CLASS_NUM_SUFFIX_RE = re.compile(r"\d+$")
# 2020~2021 的旧版档期页用无后缀类名（title_cn / broadcast / link_a），
# 2022 起才改成 _r 结尾，两种写法都归一。
CLASS_R_SUFFIX_RE = re.compile(r"_r$")


def normalize_class_token(token: str) -> str:
    """类名归一：title_cn_r3 / title_cn_r / title_cn 全部 -> title_cn。"""
    return CLASS_R_SUFFIX_RE.sub("", CLASS_NUM_SUFFIX_RE.sub("", token))


def class_tokens(attr_dict: dict) -> set[str]:
    """把 class 属性拆成归一化后的类名集合。"""
    return {
        normalize_class_token(token)
        for token in (attr_dict.get("class") or "").split()
        if token
    }


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
    - p.title_cn_r / p.title_jp_r / p.broadcast_r / p.broadcast_ex_r 的文本
      （类名可能带数字序号，如 title_cn_r1，见 normalize_class_token）；
    - td.type_[a-e]_r 的作品类型与 td.type_tag_r（同含 type_tag_r1 变体）的题材标签；
    - td.link_a_r / td.link_b_r 内的全部 <a> 链接（文本 + href）；
    - 块内第一个 width 含 180 的 <img> 的 data-src（海报）。
    """

    CAPTURE_P_CLASSES = {"title_cn", "title_jp", "broadcast", "broadcast_ex"}
    LINK_CELL_CLASSES = {"link_a", "link_b"}
    # 作品类型（type_a 原创 / type_b 漫改 / type_c 小说改 / type_d、type_e 其他）
    # 与题材标签（type_tag，仅新版页面有）所在的 td 单元格。
    INFO_CELL_CLASSES = {"type_tag"} | {f"type_{letter}" for letter in "abcde"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: dict[str, list[str]] = {
            name: [] for name in self.CAPTURE_P_CLASSES | self.INFO_CELL_CLASSES
        }
        self.links: list[tuple[str, str]] = []  # (锚文本, href)
        self.poster: str | None = None
        self._capture: str | None = None  # 正在收集的 p 类名
        self._capture_depth = 0
        self._buf: list[str] = []
        self._cell_capture: str | None = None  # 正在收集的 td 单元格类名（类型/标签）
        self._cell_buf: list[str] = []
        self._in_link_cell = False
        self._anchor: list | None = None  # [href, 文本缓冲]

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes = class_tokens(attr_dict)

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
            elif tag == "br":
                self._buf.append(" ")  # 多行标题/标签里的 <br> 转空格，避免文字粘连
            return
        if self._cell_capture is not None:
            if tag == "br":
                self._cell_buf.append(" ")
            return  # 类型/标签单元格内只有文本与 <br>，无嵌套单元格

        matched_p = classes & self.CAPTURE_P_CLASSES
        matched_cell = classes & self.INFO_CELL_CLASSES
        matched_link = classes & self.LINK_CELL_CLASSES

        if tag == "p" and matched_p:
            self._capture = next(iter(matched_p))
            self._capture_depth = 1
            self._buf = []
        elif tag == "td" and matched_cell:
            self._cell_capture = next(iter(matched_cell))
            self._cell_buf = []
        elif tag == "td" and matched_link:
            self._in_link_cell = True
        elif tag == "a" and self._in_link_cell:
            href = (attr_dict.get("href") or "").strip()
            if href:
                self._anchor = [href, []]

    def handle_endtag(self, tag):
        if self._anchor is not None and tag == "a":
            self.links.append((clean_text("".join(self._anchor[1])) or "", self._anchor[0]))
            self._anchor = None
        if self._cell_capture is not None and tag == "td":
            self.texts[self._cell_capture].append("".join(self._cell_buf))
            self._cell_capture = None
            self._cell_buf = []
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
        if self._cell_capture is not None:
            self._cell_buf.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def finalize(self):
        """解析结束时调用：把未正常闭合的捕获缓冲落袋（容忍畸形 HTML）。"""
        if self._cell_capture is not None:
            self.texts[self._cell_capture].append("".join(self._cell_buf))
            self._cell_capture = None
            self._cell_buf = []
        if self._capture is not None:
            self.texts[self._capture].append("".join(self._buf))
            self._capture = None
        if self._anchor is not None:
            self.links.append((clean_text("".join(self._anchor[1])) or "", self._anchor[0]))
            self._anchor = None


def _first_text(texts: dict[str, list[str]], name: str) -> str | None:
    """取某类名收集到的第一段非空文本（存在多段时只认第一段）。"""
    for value in texts.get(name, []):
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return None


def parse_entry_chunk(
    chunk_html: str,
    fallback_image: str | None = None,
    *,
    allow_embedded_image: bool = True,
    slot_index: dict | None = None,
) -> ParsedEntry:
    """解析单个条目块为 ParsedEntry；字段缺失时保留为 None，不猜测。

    allow_embedded_image=False 时忽略块内 <img>（回退切块的块尾可能带着
    下一条目的海报），只使用 fallback_image。

    slot_index 为星期区播出位索引（build_slot_index 的产物）：条目自身的
    broadcast_r 说明里没有 HH:MM（形如“10/3周六深夜”）时，按海报 URL 关联、
    规范化标题兜底，回填具体播出时间；关联不上保持 None。同一关联还会把
    星期区分区域平台（tr_area）合并进 platforms（条目自带平台优先）。
    """
    parser = _EntryParser()
    try:
        parser.feed(chunk_html)
        parser.close()
    except Exception as exc:  # html.parser 对畸形输入宽容，这里兜底防御
        raise YucFetchError(f"条目 HTML 解析异常：{exc}") from exc
    parser.finalize()

    title_cn = _first_text(parser.texts, "title_cn")
    title_jp = _first_text(parser.texts, "title_jp")

    broadcast_parts = []
    if parser.texts["broadcast"]:
        part = clean_text(parser.texts["broadcast"][0])
        if part:
            broadcast_parts.append(part)
    if parser.texts["broadcast_ex"]:
        part = clean_text(parser.texts["broadcast_ex"][0])
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

    # 本季站点不在条目里放播放平台链接，只在星期区 tr_area 给分区域平台
    # （港台/环大陆）：按海报/标题关联后合并，条目自带平台优先。
    image = (parser.poster if allow_embedded_image else None) or fallback_image
    slot = (
        lookup_slot(slot_index, poster_url=image, title=title_cn or title_jp)
        if slot_index
        else None
    )
    for link in slot.platforms if slot else []:
        if link["url"] not in seen_urls:
            platforms.append(link)
            seen_urls.add(link["url"])

    # 作品类型散落在 type_a_r ~ type_e_r 之一，取第一段非空；题材标签在 type_tag_r。
    work_type = next(
        (
            value
            for letter in "abcde"
            if (value := _first_text(parser.texts, f"type_{letter}"))
        ),
        None,
    )
    work_tags = _first_text(parser.texts, "type_tag")

    broadcast_time = extract_broadcast_time(broadcast_note)
    if broadcast_time is None and slot is not None:
        broadcast_time = slot.time
    return ParsedEntry(
        title_cn=title_cn,
        title_jp=title_jp,
        weekday=extract_weekday(broadcast_note),
        broadcast_time=broadcast_time,
        broadcast_note=limit_text(broadcast_note),
        image_url=limit_text(image, 500),
        platforms=platforms,
        work_type=work_type,
        work_tags=work_tags,
    )


# ---------- 星期区播出位（具体时间与平台来源） ----------

# 页面下方按星期分组的播出位：<div class="div_date"> 内含 <p class="imgtext4">24:35~</p>
# 与海报；紧随其后的 <td class="date_title_">/date_title__</td> 是作品名，
# 再往下的 tr_area 行是分区域播放平台（港台/环大陆，本季条目里不放平台链接）。
# 另有一种 div_date_（尾部多一个下划线）：纯平台卡片（Netflix 等），没有时间。
SLOT_TIME_CLASSES = {"imgtext4", "imgtext5"}
SLOT_TITLE_CLASSES = {"date_title_", "date_title__"}
SLOT_DATE_CLASSES = {"div_date", "div_date_"}
SLOT_AREA_CLASS = "area"
WEEKDAY_TIME_RE = re.compile(r"^\s*(\d{1,2}:\d{2})")


@dataclass
class WeekdaySlot:
    """星期区一个播出位：具体时间 + 海报 + 标题 + 区域平台，用于回填条目信息。"""

    time: str | None  # HH:MM（可能 24:00+ 深夜时段），无时间为 None
    poster_url: str | None
    title: str | None
    platforms: list[dict] = field(default_factory=list)  # 区域平台 [{name, url}]


class _WeekdaySlotParser(html.parser.HTMLParser):
    """扫描整个档期页，提取星期区播出位（div_date/div_date_ 块 + 标题 + 区域平台）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.slots: list[WeekdaySlot] = []
        self._slot: dict | None = None  # 进行中的播出位 {time, poster_url, title, platforms}
        self._collecting: str | None = None  # None | "time" | "title" | "area"
        self._buf: list[str] = []
        self._pending_anchor: str | None = None  # 待确认的区域平台链接

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes = set((attr_dict.get("class") or "").split())
        if self._collecting == "title":
            return  # 标题单元格内只有文本（可能有 <br>）
        if tag == "div" and (classes & SLOT_DATE_CLASSES):
            # 新播出位开始：上一个（没等到标题的）直接丢弃
            self._slot = {"time": None, "poster_url": None, "title": None, "platforms": []}
            self._pending_anchor = None
            return
        if self._slot is None:
            return
        if tag == "p" and (classes & SLOT_TIME_CLASSES):
            self._collecting = "time"
            self._buf = []
        elif tag == "p" and SLOT_AREA_CLASS in classes:
            self._collecting = "area"
            self._buf = []
        elif tag == "img" and not self._slot["poster_url"]:
            src = attr_dict.get("data-src") or attr_dict.get("src")
            if src:
                self._slot["poster_url"] = src.strip()
        elif tag == "td" and (classes & SLOT_TITLE_CLASSES):
            self._collecting = "title"
            self._buf = []
        elif tag == "a":
            href = (attr_dict.get("href") or "").strip()
            self._pending_anchor = href or None

    def handle_endtag(self, tag):
        if self._collecting == "time" and tag == "p":
            match = WEEKDAY_TIME_RE.match("".join(self._buf))
            if match:
                self._slot["time"] = match.group(1)
            self._collecting = None
            self._buf = []
        elif self._collecting == "area" and tag == "p":
            self._emit_region_link("".join(self._buf))
            self._collecting = None
            self._buf = []
        elif self._collecting == "title" and tag == "td":
            self._slot["title"] = clean_text("".join(self._buf))
            self.slots.append(WeekdaySlot(**self._slot))
            self._collecting = None
            self._buf = []
        elif tag == "a":
            self._pending_anchor = None

    def _emit_region_link(self, area_text: str):
        """区域平台链接：只有锚内带 <p class="area"> 才算，复用平台域名白名单过滤。"""
        if not self._pending_anchor:
            return
        platform = platform_from_link(area_text, self._pending_anchor)
        if platform:
            self._slot["platforms"].append(platform)

    def handle_data(self, data):
        if self._collecting:
            self._buf.append(data)


def parse_weekday_slots(page_html: str) -> list[WeekdaySlot]:
    """从档期页提取星期区全部播出位（按页面出现顺序）。"""
    parser = _WeekdaySlotParser()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception as exc:  # html.parser 宽容畸形输入，这里兜底防御
        raise YucFetchError(f"档期页星期区解析异常：{exc}") from exc
    return parser.slots


def build_slot_index(slots: list[WeekdaySlot]) -> dict:
    """把播出位按 海报URL / 规范化标题 建成索引，供条目回填时间与平台。

    同一键多次出现时保留首个；但首个没有时间而后续有时，换成有时间的
    （同作品多时段取首个带时间的播出位）。
    """
    index = {"by_poster": {}, "by_title": {}}
    for slot in slots:
        registrations = []
        if slot.poster_url:
            registrations.append(("by_poster", slot.poster_url))
        title_key = normalize_title_key(slot.title)
        if title_key:
            registrations.append(("by_title", title_key))
        for bucket, key in registrations:
            current = index[bucket].get(key)
            if current is None or (current.time is None and slot.time is not None):
                index[bucket][key] = slot
    return index


def lookup_slot(
    slot_index: dict, *, poster_url: str | None, title: str | None
) -> WeekdaySlot | None:
    """按海报 URL 关联、规范化标题兜底，找条目对应的播出位。"""
    if not slot_index:
        return None
    if poster_url and poster_url in slot_index["by_poster"]:
        return slot_index["by_poster"][poster_url]
    key = normalize_title_key(title)
    if key and key in slot_index["by_title"]:
        return slot_index["by_title"][key]
    return None


def slot_time_for(slot_index: dict, *, poster_url: str | None, title: str | None) -> str | None:
    """取条目对应的具体播出时间（HH:MM）；无时间播出位或关联不上返回 None。"""
    slot = lookup_slot(slot_index, poster_url=poster_url, title=title)
    return slot.time if slot else None


# ---------- 网络请求 ----------


def build_opener(proxy: str | None, insecure: bool = False) -> urllib.request.OpenerDirector:
    """按代理配置构建 opener：配置代理则走代理，否则显式直连（不读系统代理）。

    insecure=True 时跳过 TLS 证书校验。仅用于源站证书过期又必须取数的场景
    （yuc.wiki 证书 2026-07-29 到期后未续期，2026-10 起直连握手失败）；
    证书正常时不要开，开了就不再能发现中间人篡改。
    """
    if proxy:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ]
    else:
        handlers = [urllib.request.ProxyHandler({})]
    if insecure:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    return urllib.request.build_opener(*handlers)


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
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html, application/xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
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


def _has_cjk(text: str | None) -> bool:
    """主标题是否含中日韩汉字（跨语言合并时优先保留中文条目）。"""
    if not text:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _choose_best_entry(entries: list["ParsedEntry"]) -> "ParsedEntry":
    """同一作品（共享主标题或日文原名）的多条候选里优先保留信息更完整的。

    - 优先带中文译名（title_cn 非空）的；
    - 其次主标题含汉字（本身就是中文条目）的；
    - 都没有则保留首条（与旧“保留首条”行为一致）。
    """
    if len(entries) == 1:
        return entries[0]
    with_cn = [e for e in entries if e.title_cn]
    if with_cn:
        return with_cn[0]
    with_cjk = [e for e in entries if _has_cjk(e.title_main)]
    if with_cjk:
        return with_cjk[0]
    return entries[0]


# ---------- 同步主流程 ----------


def run_sync(
    season_code: str | None,
    *,
    session_factory,
    apply: bool = False,
    proxy: str | None = None,
    insecure: bool | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    delay: float = DEFAULT_DELAY,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = YUC_BASE_URL,
    fetch=None,
    sleep=time.sleep,
    season_url: str | None = None,
    season_title: str = "",
    category: str | None = None,
) -> SyncSummary:
    """同步一个档期：Atom 发现 -> 抓取档期页 -> 解析 -> 去重 -> 写库 -> 汇总。

    - fetch：可选的 fetch(url) -> str 注入点，测试用它替代真实网络；
      不传时按 proxy/timeout 等参数构造真实请求器（请求间加 delay 限频）。
    - season_url：直接给出档期页地址时跳过 Atom（回填历史档期用，Atom 只留最近
      十几个档期，2020~2022 的老档期页仍然在线但不在 Atom 里）。
    - dry-run 不写新番数据；两种模式都保存同步运行记录。
    - 单条写入失败不中断整体；同步不删除本地已有记录。
    """
    mode = "apply" if apply else "dry-run"
    insecure = resolve_insecure(insecure)
    if fetch is None:
        fetch = _make_fetcher(
            opener=build_opener(proxy, insecure), timeout=timeout, retries=retries,
            user_agent=user_agent, sleep=sleep,
        )

    if season_url:
        season = SeasonInfo(
            code=season_code or "",
            title=season_title or f"{season_code} 新番表",
            url=season_url,
        )
    else:
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

        # 具体播出时间（HH:MM）不在条目里，而在页面下方星期区播出位；
        # 先建索引，解析条目时按海报/标题回填。
        slots = parse_weekday_slots(page_html)
        slot_index = build_slot_index(slots)
        summary.weekday_slots = len(slots)

        issues: list[str] = []

        # 阶段 1：解析整页所有条目块。解析失败 / 缺主标题 / 主标题无法规范化
        # 的当场记 skipped，不进入后续合并与入库。
        parsed: list[ParsedEntry] = []
        parsed_keys: list[set[str]] = []
        for chunk in split_entries(page_html):
            summary.parsed += 1
            try:
                entry = parse_entry_chunk(
                    chunk.html,
                    fallback_image=chunk.fallback_image,
                    allow_embedded_image=chunk.trust_embedded_image,
                    slot_index=slot_index,
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

            main_key = normalize_title_key(title)
            if not main_key:
                summary.skipped += 1
                reason = f"主标题无法规范化：{title!r}"
                issues.append(reason)
                summary.outcomes.append(EntryOutcome(title=title, status="skipped", reason=reason))
                continue
            jp_key = normalize_title_key(entry.title_jp)
            parsed.append(entry)
            parsed_keys.append({main_key, jp_key} - {None})

        # 阶段 2：跨语言 / 跨写法合并。同一作品可能被页面列成多条：
        #  - 同主标题重复（渲染 bug，main 相同）；
        #  - 中日文两条（main 一中一日、title_jp 相同）。
        # 按 main_key / jp_key 任一共键用并查集归组，组内优先保留带中文译名
        # 的条目，确保最终每部作品只入库一条且尽量保留中文信息。
        parent = list(range(len(parsed)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        key_index: dict[str, list[int]] = {}
        for i, ks in enumerate(parsed_keys):
            for k in ks:
                key_index.setdefault(k, []).append(i)
        for idxs in key_index.values():
            for j in idxs[1:]:
                union(idxs[0], j)

        groups: dict[int, list[int]] = {}
        for i in range(len(parsed)):
            groups.setdefault(find(i), []).append(i)

        seen_keys: set[str] = set()
        for grp in groups.values():
            members = [parsed[i] for i in grp]
            best = _choose_best_entry(members)
            best_key = normalize_title_key(best.title_main)
            if not best_key:
                summary.skipped += 1
                summary.outcomes.append(
                    EntryOutcome(title=best.title_main or "", status="skipped", reason="主标题无法规范化")
                )
                continue
            if best_key in seen_keys:
                # 不同日文名却撞了主标题键（极少见）：整组判重复，全部丢弃。
                for member in members:
                    summary.duplicates += 1
                    summary.outcomes.append(
                        EntryOutcome(
                            title=member.title_main or "",
                            status="duplicate",
                            reason="同档期内重复作品，仅保留首条",
                        )
                    )
                continue
            seen_keys.add(best_key)
            summary.valid += 1
            if len(grp) > 1:
                summary.duplicates += len(grp) - 1
                for member in members:
                    if normalize_title_key(member.title_main) == best_key:
                        continue
                    summary.outcomes.append(
                        EntryOutcome(
                            title=member.title_main or "",
                            status="duplicate",
                            reason="中日文/重复作品，已合并保留中文译名条目",
                        )
                    )

            entry = best
            title = entry.title_main
            data = {
                "season_code": season.code,
                "source_key": limit_text(best_key),
                "title_main": limit_text(title),
                "title_cn": limit_text(entry.title_cn),
                "title_jp": limit_text(entry.title_jp),
                "weekday": entry.weekday,
                "broadcast_time": limit_text(entry.broadcast_time, 20),
                "broadcast_note": limit_text(entry.broadcast_note),
                "image_url": entry.image_url,
                "platforms": entry.platforms,
                "work_type": limit_text(entry.work_type, 50),
                "work_tags": limit_text(entry.work_tags, 200),
                "source_url": season.url,
                "category": category,
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


# ---------- 特殊档期分类同步 ----------

# 特殊分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）不在季度档期发现
# 路径里：它们的 season_code 就是分类码，必须先从 Atom 定位页面地址再抓。
SPECIAL_CATEGORY_CODES = tuple(c["code"] for c in SPECIAL_CATEGORIES)


def find_special_category(atom_text: str, code: str) -> SeasonInfo:
    """从 Atom 中定位某个特殊分类的页面地址与展示名。

    找不到时抛 SeasonNotFoundError（带上已发现的分类列表，便于回显原因）。
    """
    found = {s.code: s for s in parse_special_categories(atom_text)}
    info = found.get(code)
    if info is None:
        raise SeasonNotFoundError(code, list(found.values()))
    return info


def run_sync_special_category(
    code: str,
    *,
    session_factory,
    apply: bool = False,
    base_url: str = YUC_BASE_URL,
    fetch=None,
    **kwargs,
) -> SyncSummary:
    """同步一个特殊档期分类：读 Atom 定位分类页 -> 抓页 -> 解析 -> 带 category 写库。

    与 run_sync 的区别只在于「目标页面从哪来」：季度档期由 Atom 的 /YYYYMM/
    路径发现，特殊分类由分类码定位（分类码同时作为 season_code 与 category 落库）。
    """
    if fetch is None:
        fetch = _make_fetcher(
            opener=build_opener(kwargs.pop("proxy", None), resolve_insecure(kwargs.pop("insecure", None))),
            timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT),
            retries=kwargs.pop("retries", DEFAULT_RETRIES),
            user_agent=kwargs.pop("user_agent", DEFAULT_USER_AGENT),
            sleep=kwargs.pop("sleep", time.sleep),
        )
    atom_text = fetch(f"{base_url.rstrip('/')}/atom.xml")
    info = find_special_category(atom_text, code)
    return run_sync(
        code,
        session_factory=session_factory,
        apply=apply,
        base_url=base_url,
        fetch=fetch,
        season_url=info.url,
        season_title=info.title,
        category=code,
        **kwargs,
    )


# ---------- 历史档期回填 ----------

# yuc.wiki 只在 1/4/7/10 月出表，其余月份没有档期页。
QUARTER_MONTHS = ("01", "04", "07", "10")


def quarter_seasons(
    start_code: str, *, end_code: str | None = None, now: datetime | None = None
) -> list[str]:
    """从 start_code 到 end_code（默认最新季度）的季度档期码，按新到旧排序。

    Atom 只保留最近十几个档期（2023 起），2020~2022 的老档期页仍在线但不在
    Atom 里，回填必须按「年-季度」直接拼 URL 去抓。
    """
    match = re.fullmatch(r"(20\d{2})(0[1-9]|1[0-2])", start_code or "")
    if not match:
        raise ValueError(f"回填起点需为 YYYYMM（收到 {start_code!r}）")
    clock = now or datetime.now()
    end_year, end_month = (
        (int(end_code[:4]), int(end_code[4:])) if end_code else (clock.year, clock.month)
    )

    codes: list[str] = []
    year, month = int(match.group(1)), int(match.group(2))
    while (year, month) <= (end_year, end_month):
        code = f"{year:04d}{month:02d}"
        if f"{month:02d}" in QUARTER_MONTHS:
            codes.append(code)
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return sorted(codes, reverse=True)


def run_sync_backfill(
    codes: list[str],
    *,
    session_factory,
    apply: bool = False,
    base_url: str = YUC_BASE_URL,
    fetch=None,
    delay: float = DEFAULT_DELAY,
    sleep=time.sleep,
    **kwargs,
) -> list[SyncSummary]:
    """按给定档期码逐个直接抓档期页（不经 Atom）；单个失败不影响其余。"""
    summaries: list[SyncSummary] = []
    for index, code in enumerate(codes):
        url = f"{base_url.rstrip('/')}/{code}/"
        try:
            summaries.append(
                run_sync(
                    code,
                    session_factory=session_factory,
                    apply=apply,
                    base_url=base_url,
                    fetch=fetch,
                    delay=delay,
                    sleep=sleep,
                    season_url=url,
                    **kwargs,
                )
            )
        except (YucFetchError, SeasonNotFoundError) as exc:
            failed = SyncSummary(
                mode="apply" if apply else "dry-run",
                season_code=code,
                season_title="",
                season_url=url,
            )
            failed.failed = 1
            failed.outcomes.append(EntryOutcome(title="", status="failed", reason=str(exc)))
            summaries.append(failed)
        if delay > 0 and index + 1 < len(codes):
            sleep(delay)
    return summaries


def run_sync_seasons(
    season_codes: list[str],
    *,
    session_factory,
    apply: bool = False,
    proxy: str | None = None,
    insecure: bool | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    delay: float = DEFAULT_DELAY,
    user_agent: str = DEFAULT_USER_AGENT,
    fetch=None,
    sleep=time.sleep,
) -> list[SyncSummary]:
    """按给定顺序同步多个档期（回填历史档期用），每个档期一份汇总。

    单个档期失败（档期不存在 / 页面请求失败）不中断整体，失败原因记在该档期的
    outcomes 里；fetch 传入时多个档期复用同一个请求器，档期之间按 delay 限频。
    """
    insecure = resolve_insecure(insecure)
    summaries: list[SyncSummary] = []
    for index, code in enumerate(season_codes):
        try:
            summaries.append(
                run_sync(
                    code,
                    session_factory=session_factory,
                    apply=apply,
                    proxy=proxy,
                    insecure=insecure,
                    timeout=timeout,
                    retries=retries,
                    delay=delay,
                    user_agent=user_agent,
                    fetch=fetch,
                    sleep=sleep,
                )
            )
        except (YucFetchError, SeasonNotFoundError) as exc:
            failed = SyncSummary(
                mode="apply" if apply else "dry-run",
                season_code=code,
                season_title="",
                season_url="",
            )
            failed.failed = 1
            failed.outcomes.append(EntryOutcome(title="", status="failed", reason=str(exc)))
            summaries.append(failed)
        if delay > 0 and index + 1 < len(season_codes):
            sleep(delay)
    return summaries


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
        "--seasons",
        type=int,
        default=0,
        metavar="N",
        help="同步 Atom 中最近的 N 个档期（与 --season 互斥，用于回填历史档期）",
    )
    parser.add_argument(
        "--backfill-since",
        default=None,
        metavar="YYYYMM",
        help=(
            "从该季度起回填到最新一期（如 202001）：直接抓档期页、不依赖 Atom，"
            "只认同 1/4/7/10 月的季度档期；与 --season/--seasons 互斥"
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=None,
        help="跳过 TLS 证书校验（源站证书过期又必须取数时临时使用；也可设 YUC_INSECURE=1）",
    )
    parser.add_argument(
        "--category",
        default=None,
        metavar="CODE",
        help=(
            "特殊档期分类码（unreleased / ova_sp / cn_movie）；与 --season/--seasons/"
            "--backfill-since 互斥，按 Atom 改写后的本站点名抓取对应分类页"
        ),
    )
    parser.add_argument(
        "--special",
        action="store_true",
        help=(
            "同步全部三个特殊档期分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）；"
            "与 --season/--seasons/--backfill-since 互斥"
        ),
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


def resolve_insecure(explicit: bool | None, env=None) -> bool:
    """证书校验降级取值：显式参数 > YUC_INSECURE 环境变量 > 关。

    yuc.wiki 证书 2026-07-29 到期后未续期，服务器 cron 与管理端同步需要降级取数；
    站点续期后把 YUC_INSECURE 去掉即可恢复严格校验。
    """
    if env is None:
        env = os.environ
    if explicit is not None:
        return explicit
    return str(env.get("YUC_INSECURE", "")).strip().lower() in {"1", "true", "yes", "on"}


def build_session_factory(database_url: str, busy_timeout: float = 30.0):
    """按数据库 URL 建会话工厂（SQLite 放宽写锁等待，容忍与线上服务并发写）。

    老库可能还没有 work_type / work_tags 列（无 Alembic，靠 crud 里的
    ensure_* 幂等补列），先补列再同步，避免写入即报“no such column”。

    返回 (session_factory, engine)：调用方负责 engine.dispose()。
    """
    engine = build_engine(database_url, busy_timeout=busy_timeout)
    crud.ensure_schedule_work_info_columns(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


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
    if args.season and args.seasons:
        print("错误：--season 与 --seasons 不能同时使用")
        return 2
    if args.backfill_since and (args.season or args.seasons):
        print("错误：--backfill-since 不能与 --season / --seasons 同时使用")
        return 2

    special_codes: list[str] = []
    if args.special:
        special_codes = [c["code"] for c in SPECIAL_CATEGORIES]
    elif args.category:
        if args.category not in {c["code"] for c in SPECIAL_CATEGORIES}:
            print(
                "错误：未知的特殊档期分类码："
                f"{args.category!r}（可选：unreleased / ova_sp / cn_movie）"
            )
            return 2
        special_codes = [args.category]
    if special_codes and (args.season or args.seasons or args.backfill_since):
        print("错误：--category / --special 不能与 --season / --seasons / --backfill-since 同时使用")
        return 2

    fetch = _make_fetcher(
        opener=build_opener(proxy, args.insecure), timeout=args.timeout, retries=args.retries,
        user_agent=args.user_agent,
    )

    print(f"代理：{proxy if proxy else '未启用（直连）'}")
    if args.insecure:
        print("证书校验：已跳过（--insecure）")

    # 回填走直接档期页地址，不需要先读 Atom（Atom 只有最近十几个档期）。
    backfill_codes: list[str] = []
    if args.backfill_since:
        try:
            backfill_codes = quarter_seasons(args.backfill_since)
        except ValueError as exc:
            print(f"错误：{exc}")
            return 2
        if not backfill_codes:
            print(f"错误：{args.backfill_since} 之后没有可回填的季度档期")
            return 2
        print(
            f"回填 {len(backfill_codes)} 个档期：{backfill_codes[-1]} ~ {backfill_codes[0]}"
        )
        seasons: list[SeasonInfo] = []
    else:
        try:
            seasons = parse_atom(fetch(ATOM_URL))
        except YucFetchError as exc:
            print(f"错误：{exc}")
            return 1

    if args.list_seasons:
        print_seasons(seasons)
        return 0

    special_map: dict[str, SeasonInfo] = {}
    if special_codes:
        try:
            special_map = {s.code: s for s in parse_special_categories(fetch(ATOM_URL))}
        except YucFetchError as exc:
            print(f"错误：{exc}")
            return 1
        missing = [c for c in special_codes if c not in special_map]
        if missing:
            print(f"错误：Atom 中未发现特殊档期分类：{', '.join(missing)}")
            return 1
        print(
            f"同步 {len(special_codes)} 个特殊档期分类："
            f"{'、'.join(special_map[c].title for c in special_codes)}"
        )
    if not backfill_codes and not seasons and not special_codes:
        print("错误：Atom 中未发现任何档期页面，无法同步")
        return 1

    database_url = args.database_url or settings.database_url
    print(f"数据库：{database_url}")
    session_factory, engine = build_session_factory(database_url, busy_timeout=60.0)
    summaries: list[SyncSummary] = []
    try:
        if backfill_codes:
            summaries = run_sync_backfill(
                backfill_codes,
                session_factory=session_factory,
                apply=args.apply,
                proxy=proxy,
                insecure=args.insecure,
                timeout=args.timeout,
                retries=args.retries,
                delay=args.delay,
                user_agent=args.user_agent,
                fetch=fetch,
            )
        elif args.seasons:
            codes = [season.code for season in seasons[: args.seasons]]
            print(f"批量同步最近 {len(codes)} 个档期：{'、'.join(codes)}")
            summaries = run_sync_seasons(
                codes,
                session_factory=session_factory,
                apply=args.apply,
                proxy=proxy,
                insecure=args.insecure,
                timeout=args.timeout,
                retries=args.retries,
                delay=args.delay,
                user_agent=args.user_agent,
                fetch=fetch,
            )
        elif special_codes:
            for code in special_codes:
                info = special_map[code]
                try:
                    summaries.append(
                        run_sync(
                            code,
                            session_factory=session_factory,
                            apply=args.apply,
                            proxy=proxy,
                            insecure=args.insecure,
                            timeout=args.timeout,
                            retries=args.retries,
                            delay=args.delay,
                            user_agent=args.user_agent,
                            fetch=fetch,
                            season_url=info.url,
                            season_title=info.title,
                            category=code,
                        )
                    )
                except (YucFetchError, SeasonNotFoundError) as exc:
                    failed = SyncSummary(
                        mode="apply" if args.apply else "dry-run",
                        season_code=code,
                        season_title=info.title,
                        season_url=info.url,
                    )
                    failed.failed = 1
                    failed.outcomes.append(
                        EntryOutcome(title="", status="failed", reason=str(exc))
                    )
                    summaries.append(failed)
                if args.delay > 0 and code != special_codes[-1]:
                    time.sleep(args.delay)
        else:
            try:
                summaries = [
                    run_sync(
                        args.season,
                        session_factory=session_factory,
                        apply=args.apply,
                        proxy=proxy,
                        insecure=args.insecure,
                        timeout=args.timeout,
                        retries=args.retries,
                        delay=args.delay,
                        user_agent=args.user_agent,
                        fetch=fetch,
                    )
                ]
            except SeasonNotFoundError as exc:
                print(f"错误：{exc}")
                print_seasons(exc.available)
                return 2
    finally:
        engine.dispose()

    total_failed = 0
    for summary in summaries:
        for outcome in summary.issues:
            label = {"duplicate": "重复", "skipped": "跳过", "failed": "失败"}[outcome.status]
            parts = [f"[{label}]"]
            if outcome.title:
                parts.append(f"《{outcome.title}》")
            if outcome.reason:
                parts.append(f"- {outcome.reason}")
            print(" ".join(parts))
        print(summary.describe())
        total_failed += summary.failed
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
