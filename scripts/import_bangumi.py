"""Bangumi 条目导入脚本：从 Bangumi 官方 API 拉取条目，映射为 Content 记录。

设计要点（给后续维护者）：
- 默认只预览：不写数据库；显式加 --apply 才正式入库。
- 代理可配置：--proxy 参数或 BANGUMI_PROXY 环境变量，默认直连（不读取系统代理）。
- 只用 Python 标准库（urllib），不新增第三方依赖。
- 重复导入安全：bangumi_id 已存在时直接跳过，不覆盖本地数据。
- 类型映射覆盖本站四类型：type=1（novel）、2（anime）、4（game）、6（comic）；
  音乐（3）与三次元（5）明确跳过。注意「书籍」分区里小说与漫画混在一起，
  必须按条目 type 区分，不能按分区归类。
- rating 只取 API 返回的有效评分（rating.score > 0），取不到就跳过，不伪造。
- **标签随作品一起导入**：取 API tags 中标注数（count）最多的前
  ``MAX_TAGS_PER_SUBJECT`` 个**题材**标签（经 ``app.tagging.classify_tag``
  归一：同义词归并、年份/公司/声优/媒介/平台在入库前分流——公司进
  content_studios、年份进 contents.year），经 ``crud.replace_content_tags``
  替换式写入（不复制标签逻辑，与站内筛选共用同一套表）；标签是可选增强，
  解析不到不影响作品本身入库。
- **作者/公司关系随作品一起识别**：从条目信息卡（infobox）按白名单识别
  原作 / 导演 / 动画制作等角色，经 ``crud.replace_content_entity_links``
  替换式写入（见 app/entity_links.py）；信息卡没有可用键时静默跳过，
  不影响作品本身入库。存量作品补识别用 scripts/link_entities.py。
- **封面随作品一起落地**（--apply 时）：取 API images 里最大的一张图下载到
  ``cover_storage_dir``（见 app/config.py），``Content.image_url`` 存站内相对
  地址（/covers/cover_{bangumi_id}.jpg），页面不再热链 Bangumi。下载失败
  不阻塞作品入库，只在结果里注明原因；``COVER_DOWNLOAD_ENABLED=false`` 或
  ``--no-cover`` 时完全跳过下载。已有库补封面用 scripts/fetch_covers.py。

用法示例：
    # 预览（默认，不写库）
    python scripts/import_bangumi.py 265 975
    # 正式入库（含封面本地化）
    python scripts/import_bangumi.py 265 975 --apply
    # 走本机代理
    python scripts/import_bangumi.py 265 --proxy http://127.0.0.1:9910
    # 或用环境变量
    set BANGUMI_PROXY=http://127.0.0.1:9910 && python scripts/import_bangumi.py 265
    # 只导数据不下图
    python scripts/import_bangumi.py 265 --apply --no-cover

本文件同时是可复用模块：核心函数（map_subject / fetch_subject / run_import）
可被测试或后续功能直接调用，见 tests/test_bangumi_import.py。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
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

from app import crud  # noqa: E402
from app import entity_links  # noqa: E402
from app.bangumi_meta import _clean_name, infobox_map  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Content  # noqa: E402
from app.tagging import (  # noqa: E402
    TAG_STUDIO,
    TAG_YEAR,
    canonical_genre,
    classify_tag,
    is_genre_candidate,
    year_from_date,
)

# ---------- 常量与配置 ----------

# Bangumi API 基址：默认官方站点；服务器直连被 DNS 污染时可用环境变量指向镜像
# （如 BANGUMI_API_BASE=https://bgmapi.anibt.net，字节级兼容 v0 接口），无需改代码。
BANGUMI_API_BASE = os.environ.get("BANGUMI_API_BASE", "https://api.bgm.tv").rstrip("/")
SOURCE_URL_TEMPLATE = "https://bgm.tv/subject/{subject_id}"
DEFAULT_USER_AGENT = "acgn-test-platform/0.1 (local import script; github.com/acgn-test-platform)"
DEFAULT_TIMEOUT = 10.0  # 单次请求超时（秒）
DEFAULT_RETRIES = 2  # 失败后的重试次数（总请求次数 = 1 + retries）

# 每个条目最多导入的标签数：源站标签常多达几十个，只保留标注数最多的前几个，
# 避免标签刷屏稀释筛选意义；与 crud.MAX_CONTENT_TAGS 保持一致（两侧各自兜底）。
MAX_TAGS_PER_SUBJECT = crud.MAX_CONTENT_TAGS
# 标签里识别出的制作公司上限（动画常见 1-2 家，多了反而是噪音）。
MAX_STUDIOS_PER_SUBJECT = 3
# 书籍条目从 infobox 派生的标签上限（文库 / 丛书，补轻小说区筛选维度）。
MAX_BOOK_TAGS_PER_SUBJECT = 3

# Bangumi 条目类型 -> 本站 Content.type。
#
# 实测归档（2026-10-13 dump）确认的真实语义：
# - type=2 动画、type=4 游戏，直接映射；
# - type=1 书籍区（42 万条）里漫画与小说混在一起，**必须按 platform 拆分**：
#   1001=漫画（灌篮高手/剑风传奇/火之鸟）、1002=小说（三体/魔戒/卡拉马佐夫兄弟）、
#   1003=画集、1004=其它；
# - type=6 是三次元真人剧（一升的眼泪/白夜行），type=3 是音乐——都不属于本站
#   四类型，明确跳过。（Archive README 把 6 写成「漫画」是错的，别信。）
TYPE_MAPPING: dict[int, str] = {2: "anime", 4: "game"}
TYPE_NAMES: dict[int, str] = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 5: "三次元", 6: "真人"}

# 书籍区 platform -> content_type。数字代码来自归档；API 的 platform 是字符串
# （"漫画" / "轻小说" / "小说" 等），用 BOOK_PLATFORM_STRINGS 匹配。
BOOK_PLATFORM_MAPPING: dict[int, str] = {1001: "comic", 1002: "novel"}
BOOK_PLATFORM_STRINGS: tuple[tuple[str, str], ...] = (
    ("漫画", "comic"),
    ("小说", "novel"),
)

"""书籍细分信号（见 classify_book）：Bangumi 没有「轻小说」独立类型，只能靠标签 /
文库字段 / 日文特征推断。

两个取值都是**标注**（contents.book_kind），不是内容类型：文学不等于第五种类型。"""
LIGHT_NOVEL_TAG = "轻小说"

BOOK_KIND_LIGHT_NOVEL = "light_novel"  # 文库出身的轻小说
BOOK_KIND_LITERATURE = "literature"  # 日本普通小说（非文库轻小说）
# 已知轻小说文库（中日两种写法都收，覆盖主要几家）。判定只看「文库字段的值
# 命中这份名单」——单有「文库」字段不够（新潮文庫 / 岩波文庫 是普通文学）。
KNOWN_LIGHT_NOVEL_BUNKO: tuple[str, ...] = (
    "電撃文庫", "电击文库",
    "富士見ファンタジア文庫", "富士见幻想文库",
    "角川スニーカー文庫", "角川Sneaker文库",
    "GA文庫", "GA文库",
    "MF文庫J", "MF文库J",
    "ファミ通文庫", "Fami通文库",
    "講談社ラノベ文庫", "讲谈社轻小说文库",
    "スーパーダッシュ文庫", "Super Dash文库",
    "HJ文庫", "HJ文库",
    "富士見書房", "富士见书房",
)
# 日文假名：判断「这本书是不是日本文库/出版社出的」最短平快的信号。
_JP_KANA_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
# 中文出版社固定用「…出版社」（人民文学出版社 / 重庆出版社），日系不带「出版」二字。
CN_PUBLISHER_RE = re.compile(r"\S{0,8}出版社")
# 日系出版社 / 出版品牌（单靠假名判不出来的补位）。
JP_PUBLISHERS: tuple[str, ...] = (
    "集英社", "講談社", "讲谈社", "角川", "KADOKAWA", "小学館", "小学馆",
    "徳間書店", "德间书店", "新潮社", "岩波書店", "岩波书店", "文藝春秋", "文艺春秋",
    "早川書房", "早川书房", "双葉社", "祥伝社", "白泉社", "KADOKAWA/富士見",
)


def _iter_infobox_strings(payload: dict, *, include_keys: bool = True) -> list[str]:
    """把 infobox 摊平成字符串列表（键与值都算）。

    API 的 infobox 是 ``[{"key": "作者", "value": "xxx"}, ...]``，value 可能是
    字符串也可能是列表；这里统一取字符串来匹配，缺字段返回空列表。
    """
    raw = payload.get("infobox")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if include_keys and isinstance(item.get("key"), str):
            out.append(item["key"])
        for field in ("value",):
            value = item.get(field)
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, list):
                out.extend(str(v) for v in value if isinstance(v, (str, int, float)))
    return out


def classify_book(payload: dict) -> tuple[str | None, str | None]:
    """书籍区细分，返回 ``(content_type, book_kind)``。

    返回 ``(None, None)`` 表示**不入库**——Bangumi 的「小说」platform 覆盖的是
    所有小说，中译本、欧美古典、中国古典名著都混在里面，本站不收。
    返回 ``("novel", kind)`` 时 kind 取值：

    - ``light_novel`` 文库出身的轻小说；
    - ``literature`` 日本普通小说（纯文学 / 一般文艺）。

    为什么 kind 不写成独立类型（2026-10-19）：最初确实把文学拆成了第五种类型，
    但用户视角这两类都在「轻小说区」翻，拆类型要动导航、筛选、统计、我的空间的
    类型枚举，换来的只是一眼看出的区分——而这个区别用一枚角标就够了。
    所以类型落在 novel，book_kind 只作标注（见 app/page_common.BOOK_KIND_LABELS）。

     Bangumi 没有「轻小说」独立类型，只能靠信号判定，优先级从强到弱：

    1. tags 里出现「轻小说」；
    2. infobox 出现文库/レーベル 字段，或值命中已知轻小说文库（電撃文庫 等）；
    3. 日本出版物（日文假名 / 日系出版社）-> literature；
    4. 中文出版社写法 -> 丢弃。

    启发式的取舍：宁可把边界件判成 literature 留在站内（可见、可人工改），
    也不要漏判成轻小说污染轻小说区，更不要误删一本真日本书。
    """
    names = _tag_names(payload) + _iter_infobox_strings(payload)
    joined = "\n".join(names)
    # 「出版社」中文写法的判定只看 value：infobox 里 **键名** 就叫「出版社」，
    # 把键也算进来会让每一本书都被判成中文书。
    values = "\n".join(_iter_infobox_strings(payload, include_keys=False))
    if LIGHT_NOVEL_TAG in _tag_names(payload):
        return "novel", BOOK_KIND_LIGHT_NOVEL
    # 只认「值命中已知轻小说文库」，不再用「infobox 里有文库字段」这种弱信号：
    # 新潮文庫 / 岩波文庫 同样有文库字段，照旧判会把东野圭吾、京极夏彦、
    # 伊坂幸太郎这些普通推理文艺一并当成轻小说（线上曾因此混进十几部）。
    # 已知文库名单见 KNOWN_LIGHT_NOVEL_BUNKO（電撃文庫 / MF文庫J / GA文庫 …）。
    if any(bunko in joined for bunko in KNOWN_LIGHT_NOVEL_BUNKO):
        return "novel", BOOK_KIND_LIGHT_NOVEL
    # 「XX出版社」是中文出版社的固定写法（人民文学出版社 / 重庆出版社 …）；
    # 日系出版方收尾是 社 / 書店 / 書房，不带「出版」二字——这是性价比最高的
    # 「这本书不是日本书」信号，比猜作者国籍可靠。
    if CN_PUBLISHER_RE.search(values):
        return None, None
    if _JP_KANA_RE.search(joined) or any(pub in joined for pub in JP_PUBLISHERS):
        return "novel", BOOK_KIND_LITERATURE
    # 判不出国籍时按「留下来」处理：宁可在「文学」里多一本待复核的书，
    # 也不要误删一本真日本书（轻小说区不会被污染，这是底线）。
    return "novel", BOOK_KIND_LITERATURE


def _tag_names(payload: dict) -> list[str]:
    """全部标签名（不做截断 / 不排序）。

    刻意不走 extract_tags：那个函数只保留标注数最高的前 N 个（那是入库用的
    题材标签），而分类要的是**全量标签**——「轻小说」标签常常排在十几名之后，
    被截断掉就永远判不出来。
    """
    payload_tags = payload.get("tags")
    if not isinstance(payload_tags, list):
        return []
    out: list[str] = []
    for item in payload_tags:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"])
        elif isinstance(item, str):
            out.append(item)
    return out


# galge 信号词（对 tags 大小写不敏感）：Bangumi 游戏区的 galgame 系标签。
# 视觉小说 / visual novel 也算（2026-10-21 用户补充口径：文字冒险类一并收）。
GALGAME_TAG_RE = re.compile(
    r"gal\s*game|ギャルゲ|美少女游戏|美少女ゲーム|visual\s*novel|视觉小说",
    re.IGNORECASE,
)


def is_galge_payload(payload: dict) -> bool:
    """游戏条目是否 galge：tags 命中 galgame 系信号词即认（只读 tags，不出网）。

    只对游戏条目有意义；判定结果决定 map_subject 的收录口径（见该函数
    2026-10-21 的游戏收录规则）。
    """
    return any(GALGAME_TAG_RE.search(name) for name in _tag_names(payload))


def resolve_book(payload: dict) -> tuple[str | None, str | None]:
    """从条目 payload 解析 ``(content_type, book_kind)``。

    书籍条目额外经 ``classify_book`` 决定细分标注：轻小说 light_novel、
    日本普通小说 literature，非日本书直接丢弃（``(None, None)``）。
    """
    bgm_type = payload.get("type")
    if bgm_type in TYPE_MAPPING:
        return TYPE_MAPPING[bgm_type], None
    if bgm_type != 1:
        return None, None
    platform = payload.get("platform")
    if isinstance(platform, int) and not isinstance(platform, bool):
        if platform == 1001:
            return "comic", None
        if platform == 1002:  # 「小说」-> 再判轻小说 / 文学 / 不要
            return classify_book(payload)
        return None, None  # 画集（1003）、其它（1004）
    if isinstance(platform, str):
        text = platform.strip()
        if "漫画" in text:
            return "comic", None
        if "轻小说" in text:  # platform 直接写明，最强信号
            return "novel", BOOK_KIND_LIGHT_NOVEL
        if "小说" in text:
            return classify_book(payload)
    return None, None


def resolve_content_type(payload: dict) -> str | None:
    """``resolve_book`` 的只取类型版本，保留给只关心类型的调用方。

    - type=2 -> anime，type=4 -> game；
    - type=1 书籍按 platform 细分：1001/"漫画" -> comic；「小说」交给
      ``classify_book`` 再判一层（轻小说 / 日本文学均返回 novel，非日本书丢弃）；
    - 其余类型（3 音乐 / 5、6 三次元）与解析不出的返回 None。
    """
    content_type, _kind = resolve_book(payload)
    return content_type

# 这些 HTTP 状态码值得重试（限流或服务端临时故障）；4xx 其余状态直接失败。
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

TITLE_MAX_LENGTH = 200  # 与 Content.title 的 String(200) 一致

# ---------- 随机导入：随机偏移翻排行榜页 ----------
# Bangumi 没有「随机条目」官方接口。两条路线各有代价：
# - 随机探测条目 ID：ID 空间稀疏（删除条目留洞）+ 类型过滤，命中率只有个位数
#   百分比，拿 20 条要试几百个请求，需要请求预算 / 熔断 / 死线一整套护栏；
# - 攒头部候选池再抽样（旧方案）：只覆盖排行榜头部几百条，库越长越像 Top 榜
#   大杂烩，而且重复发现要等到入库才发生，「随机 20 条」实际可能只新增几条。
# 现方案：排行榜接口带分页与 total，先取 offset=0 学习该类型榜单总量，再在
# [0, total-limit] 里随机抽偏移取页——一页 30 个保证有效、类型正确的候选 ID，
# 本地过滤已入库与 nsfw 后抽样。每拿 20 条只要 2-3 个请求，护栏只剩页数上限。
# 候选只取本站支持的类型（1=书籍/小说、2=动画、4=游戏、6=漫画），与 TYPE_MAPPING 保持一致。

RANK_PAGE_LIMIT = 30  # 排行榜单页条数
RANK_SORT = "rank"  # 固定用综合排序：偏移随机化后，排序本身不再影响覆盖面
RANK_SLEEP = 0.3  # 翻页间歇（秒），避免触发限流
RANDOM_PAGE_BUDGET = 10  # 单次随机导入最多请求多少页排行榜（10 页 = 300 个候选）
RANDOM_PAGE_SHARE = 15  # 每页计划贡献的抽样数：count 超过它就多翻几页混开批内多样性

# ---------- 封面图 ----------

# Bangumi images 字段里的尺寸键，按偏好顺序取。
#
# **large（原图）优先**（2026-10-19 实测修正）：服务器侧用的是镜像
# bgmimg.anibt.net，它只放行原图路径，所有 ``/r/<像素>/`` 缩略路径一律
# 403（medium=r/800、common=r/400 在镜像上同样受限，实测只有原图 200）。
# 旧顺序 medium 优先时，抓图端每条都拿到 403，线上 4449 部作品因此长期没封面。
# 原图 200 KB 上下，站内再由 /media/full 压成 ≤1200px/≤400KB 的 WebP，
# 多花这点带宽换「每张图都真的下得来」。
COVER_SIZE_PREFERENCE = ("large", "common", "medium")

# 封面文件大小上限：Bangumi 封面一般几十~几百 KB，5 MB 足够兜底异常响应。
COVER_MAX_BYTES = 5 * 1024 * 1024

# 允许的扩展名白名单（按 URL 路径后缀判断，不在表内一律按 .jpg 存）。
COVER_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# 简易文件魔数校验：避免把 HTML 错误页 / 限流页当成图片存下来。
COVER_IMAGE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class CoverDownloadError(RuntimeError):
    """封面下载或校验失败（网络错误、超限、响应不是图片等）。"""


def extract_cover_url(payload: dict) -> str | None:
    """从 Bangumi 条目 JSON 提取封面地址：按 COVER_SIZE_PREFERENCE 取第一张可用图。

    - images 形如 ``{"large": url, "common": url, "medium": url, ...}``；
    - 按 COVER_SIZE_PREFERENCE 顺序找第一个非空 http(s) 地址（medium 优先，省带宽）；
    - 字段缺失 / 不是字典 / 全部为空时返回 None——封面是可选增强，
      取不到不影响作品本身入库（模板回退渐变占位图）。
    """
    images = payload.get("images")
    if not isinstance(images, dict):
        return None
    for size in COVER_SIZE_PREFERENCE:
        url = images.get(size)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def cover_filename(remote_url: str, bangumi_id: int) -> str:
    """由远端地址与条目 ID 生成本地封面文件名（扩展名走白名单）。"""
    path = urllib.parse.urlsplit(remote_url).path
    suffix = Path(path).suffix.lower()
    if suffix not in COVER_ALLOWED_EXTENSIONS:
        suffix = ".jpg"
    return f"cover_{bangumi_id}{suffix}"


def _looks_like_image(body: bytes) -> bool:
    """按文件头判断响应体是否为图片（webp 需再校验 RIFF 后的 WEBP 标记）。"""
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return True
    return any(body.startswith(magic) for magic, _ in COVER_IMAGE_MAGICS)


def download_cover(
    remote_url: str,
    *,
    bangumi_id: int,
    storage_dir,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep=time.sleep,
    max_bytes: int = COVER_MAX_BYTES,
) -> str:
    """把远端封面下载到本地存储目录，返回站内相对地址（/covers/<文件名>）。

    - 与 fetch_subject 同一套重试策略（限流 / 服务端故障才重试）；
    - 超过 max_bytes 或文件头不是图片一律判失败（CoverDownloadError），
      不把错误页写成 .jpg；
    - 失败抛异常由调用方处置：封面是可选增强，绝不阻塞作品入库。
    """
    if not isinstance(remote_url, str) or not remote_url.startswith(("http://", "https://")):
        raise CoverDownloadError(f"封面地址不合法：{remote_url!r}")

    storage = Path(storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    filename = cover_filename(remote_url, bangumi_id)
    destination = storage / filename

    last_reason = "未知错误"
    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 5))
        try:
            request = urllib.request.Request(
                remote_url,
                headers={"User-Agent": user_agent, "Accept": "image/*"},
            )
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                continue
            raise CoverDownloadError(f"下载封面失败：{last_reason}（{remote_url}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_reason = f"网络错误：{exc}" if not isinstance(exc, TimeoutError) else "请求超时"
            if attempt < retries:
                continue
            raise CoverDownloadError(f"下载封面失败：{last_reason}（{remote_url}）") from exc

        if len(body) > max_bytes:
            raise CoverDownloadError(f"封面超过 {max_bytes // 1024} KB 上限，已放弃")
        if not _looks_like_image(body):
            raise CoverDownloadError("响应不是图片文件（可能是错误页），已放弃")
        destination.write_bytes(body)
        return f"/covers/{filename}"

    raise CoverDownloadError(f"下载封面失败：{last_reason}（{remote_url}）")


def _make_cover_downloader(
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float,
    retries: int,
    user_agent: str,
    sleep=time.sleep,
):
    """把 download_cover 的固定参数绑定为 download(remote_url, bangumi_id) -> 站内地址。"""

    def download(remote_url: str, bangumi_id: int) -> str:
        return download_cover(
            remote_url,
            bangumi_id=bangumi_id,
            storage_dir=settings.cover_storage_dir,
            opener=opener,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            sleep=sleep,
        )

    return download


class BangumiAPIError(RuntimeError):
    """访问 Bangumi API 失败（网络错误、超时、HTTP 错误状态等）。"""


class SubjectMappingError(ValueError):
    """API 数据无法映射为本站 Content 记录（类型不支持、必要字段缺失等）。"""


# ---------- 数据结构 ----------


@dataclass
class MappedSubject:
    """映射完成后、等待入库的一条记录（字段与 Content 一一对应）。

    tags 为随作品一起导入的**题材**标签名列表（已按标注数截断并经
    app.tagging 归一）；列表为空表示该条目没有可用题材标签，不影响作品本身入库。
    studios 为标签里识别出的制作公司（归一规范名），落到 content_studios。
    year 为发行年（条目 date 优先，标签年份兜底），落到 contents.year。
    image_url 为 Bangumi 远端封面地址（未本地化）；为空表示该条目没有封面。
    book_kind 只对小说条目有意义（light_novel / literature），是**标注**不是类型：
    类型始终是 novel，这一列只决定详情页 / 卡片上额外的「文学」角标。
    """

    bangumi_id: int
    title: str
    type: str
    summary: str
    rating: float
    source_url: str
    tags: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    year: int | None = None
    image_url: str | None = None
    book_kind: str | None = None
    # 搜索增强：日文原名（name 与 title 不同时）+ infobox 别名。
    title_original: str | None = None
    aliases: list[str] = field(default_factory=list)


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

    def as_dict(self) -> dict:
        """转成可 JSON 序列化的明细（增量入库 API 的逐条返回用，见 app/ingest_api）。"""
        return {
            "bangumi_id": self.bangumi_id,
            "status": self.status,
            "title": self.title,
            "reason": self.reason,
        }


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


@dataclass
class RandomImportResult:
    """随机导入一次运行的汇总：翻页统计 + 实际抽样 ID + 入库汇总。

    统计口径（都用于结果页回显，让命中 / 重复 / 过滤情况一目了然）：
    - pages：实际请求成功的排行榜页数（含每个类型首个用于学习 total 的页）；
    - candidates：去重后、库里没有的新候选 ID 数（抽样的总体）；
    - duplicates：因已入库被本地过滤的候选出现次数（零网络成本跳过）；
    - nsfw_skipped：页载荷显式标记 nsfw 而被过滤的条数；
    - rank_total：响应里读到的榜单总量（各类型取最大，读不到为 None），仅回显用。
    """

    requested: int
    sampled: list[int]
    summary: ImportSummary
    pages: int = 0
    candidates: int = 0
    duplicates: int = 0
    nsfw_skipped: int = 0
    rank_total: int | None = None


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


def fetch_related_subjects(
    subject_id: int,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    sleep=time.sleep,
) -> list:
    """获取单个条目的关联条目列表（/v0/subjects/{id}/subjects）。

    与 fetch_subject 同一套网络规则（UA / 超时 / 有限重试）；返回原始的
    JSON 列表，解析与防御性裁剪交给 app.entity_links.parse_related_subjects
    ——本函数不猜响应结构，只保证「拿到的是 list」。
    """
    url = f"{base_url.rstrip('/')}/v0/subjects/{int(subject_id)}/subjects"
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
            payload = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 的关联条目失败：{last_reason}（{url}）"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_reason = f"网络错误：{exc}" if not isinstance(exc, TimeoutError) else "请求超时"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 的关联条目失败：{last_reason}（{url}）"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_reason = f"响应不是有效 JSON：{exc}"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取条目 {subject_id} 的关联条目失败：{last_reason}（{url}）"
            ) from exc

        if not isinstance(payload, list):
            raise BangumiAPIError(
                f"获取条目 {subject_id} 的关联条目失败：响应不是列表（{url}）"
            )
        return payload

    raise BangumiAPIError(f"获取条目 {subject_id} 的关联条目失败：{last_reason}（{url}）")


# ---------- 排行榜随机翻页与随机导入 ----------


@dataclass
class RankPage:
    """一页排行榜的解析结果。

    - ids：本页有效条目 ID（已剔除非法值与显式标记 nsfw 的条目）；
    - total：响应里的榜单总量，读不到为 None（此时无法随机偏移，只能取首页）；
    - nsfw_skipped：因条目显式标记 nsfw 而被剔除的条数。
    """

    ids: list[int]
    total: int | None = None
    nsfw_skipped: int = 0


def _rank_item_is_nsfw(item: dict) -> bool:
    """排行榜条目是否显式标记 nsfw。

    只认 ``item.subject.nsfw`` / ``item.nsfw`` 为真的情况；字段缺失一律放行
    （不过滤），避免在响应结构跟预期不一致时误杀正常条目。
    """
    for source in (item.get("subject"), item):
        if isinstance(source, dict) and source.get("nsfw"):
            return True
    return False


def _fetch_rank_page(
    subject_type: int,
    sort: str,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    limit: int = RANK_PAGE_LIMIT,
    offset: int = 0,
    sleep=time.sleep,
) -> RankPage:
    """抓取一页排行榜并解析为 RankPage。

    与 fetch_subject 同一套网络规则（UA / 超时 / 有限重试），解析的是
    排行榜接口 ``GET /v0/rank/subject/{type}/{sort}?limit=&offset=`` 的响应：
    ``{"list": [{"subject_id": ..., "subject": {"nsfw": ...}}, ...], "total": N}``。
    subject_id 缺失时回退取 ``id``；total 非正整数按读不到处理；nsfw 只认
    显式为真的标记；响应结构不对就抛 BangumiAPIError（与正文抓取一致，不猜结构）。
    """
    url = (
        f"{base_url.rstrip('/')}/v0/rank/subject/{int(subject_type)}/{sort}"
        f"?limit={int(limit)}&offset={int(offset)}"
    )
    last_reason = "未知错误"
    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 5))
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
            )
            with opener.open(request, timeout=timeout) as response:
                body = response.read()
            payload = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取排行榜（type={subject_type}, sort={sort}）失败：{last_reason}（{url}）"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_reason = f"网络错误：{exc}" if not isinstance(exc, TimeoutError) else "请求超时"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取排行榜（type={subject_type}, sort={sort}）失败：{last_reason}（{url}）"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_reason = f"响应不是有效 JSON：{exc}"
            if attempt < retries:
                continue
            raise BangumiAPIError(
                f"获取排行榜（type={subject_type}, sort={sort}）失败：{last_reason}（{url}）"
            ) from exc

        if not isinstance(payload, dict):
            raise BangumiAPIError(
                f"获取排行榜（type={subject_type}, sort={sort}）失败：响应不是对象（{url}）"
            )
        items = payload.get("list")
        if not isinstance(items, list):
            raise BangumiAPIError(
                f"获取排行榜（type={subject_type}, sort={sort}）失败：缺少 list 字段（{url}）"
            )
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            total = None
        ids: list[int] = []
        nsfw_skipped = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if _rank_item_is_nsfw(item):
                nsfw_skipped += 1
                continue
            sid = item.get("subject_id")
            if isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0:
                sid = item.get("id")
            if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
                ids.append(sid)
        return RankPage(ids=ids, total=total, nsfw_skipped=nsfw_skipped)

    raise BangumiAPIError(
        f"获取排行榜（type={subject_type}, sort={sort}）失败：{last_reason}（{url}）"
    )


def random_import(
    *,
    session_factory,
    count: int,
    subject_types: tuple[int, ...] = (2, 4),
    apply: bool = False,
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = BANGUMI_API_BASE,
    fetch_page=None,
    fetch_subject=None,
    cover_downloader=None,
    sleep=time.sleep,
    rng=None,
) -> RandomImportResult:
    """随机抓取 ``count`` 条 Bangumi 作品并导入（预览或正式）。

    流程：随机偏移翻排行榜页 -> 本地过滤已入库 / nsfw -> 抽样 -> 复用 run_import
    入库。先取 offset=0 学习该类型的榜单总量 total，再在 [0, total-limit] 里随机
    抽偏移取页，因此覆盖榜单全深度而不只是头部；一页 30 个保证有效的候选 ID，
    拿 20 条通常只需 2-3 个请求。

    剩余护栏（比随机探测条目 ID 的请求预算 / 熔断 / 死线轻得多）：
    - 最多翻 ``RANDOM_PAGE_BUDGET`` 页；条数较多时按 ``RANDOM_PAGE_SHARE``
      拆多页抽样，避免一批作品全来自同一排名段；
    - 随机偏移失败（API 可能限制深翻页）时偏移减半回退直至 0，并记住该类型
      实际可用的偏移上限；连 offset=0 都失败视为网络不可用，连续两页失败收手；
    - 类型只有一页（total <= 单页条数）或读不到 total 时，取过一次就标记耗尽。

    参数：
    - count：目标条数（受页数预算与候选数量限制，可能拿到少于 count 的部分结果，
      统计里如实反映）；
    - fetch_page：可选的 ``fetch_page(subject_type, offset, *, limit) -> RankPage``
      注入点，测试用它替代真实排行榜请求；不传则按代理参数构造真实请求器；
    - fetch_subject：传递给 run_import 的单条目抓取注入点（同上，测试用）；
    - rng：可选随机数发生器（测试可注入固定种子，保证可复现）。
    """
    count = max(0, int(count))
    if rng is None:
        rng = random.Random()
    limit = RANK_PAGE_LIMIT

    pages = 0
    duplicates = 0
    nsfw_skipped = 0
    rank_total: int | None = None
    fresh: list[int] = []

    def _empty_result() -> RandomImportResult:
        empty = build_summary(requested=count, fetched=0, outcomes=[])
        return RandomImportResult(
            requested=count,
            sampled=[],
            summary=empty,
            pages=pages,
            candidates=len(fresh),
            duplicates=duplicates,
            nsfw_skipped=nsfw_skipped,
            rank_total=rank_total,
        )

    if count <= 0:
        return _empty_result()

    if fetch_page is None:
        opener = build_opener(proxy)

        def fetch_page(subject_type, offset, *, limit=limit):
            return _fetch_rank_page(
                subject_type,
                RANK_SORT,
                opener=opener,
                timeout=timeout,
                retries=retries,
                user_agent=user_agent,
                base_url=base_url,
                limit=limit,
                offset=offset,
                sleep=sleep,
            )

    # 本地查重：一次查询拿全库 bangumi_id，选中之前零成本跳过已入库条目
    db = session_factory()
    try:
        existing = {
            bid
            for bid in db.scalars(select(Content.bangumi_id)).all()
            if isinstance(bid, int) and not isinstance(bid, bool)
        }
    finally:
        db.close()

    types = [t for t in subject_types if isinstance(t, int) and t > 0] or [2]
    # 每页计划贡献 RANDOM_PAGE_SHARE 条 -> 至少翻 ceil(count/share) 页混开批内多样性
    min_pages = min(RANDOM_PAGE_BUDGET, max(1, -(-count // RANDOM_PAGE_SHARE)))

    type_total: dict[int, int] = {}  # 从响应学到的各类型榜单总量
    type_cap: dict[int, int] = {}  # 学到的各类型可用偏移上限（深翻页被拒时收紧）
    used_offsets: dict[int, set[int]] = {t: set() for t in types}
    exhausted: set[int] = set()  # 已无新页可翻的类型
    seen: set[int] = set()
    consecutive_failures = 0

    while True:
        if consecutive_failures >= 2:
            break  # 连续两页连 offset=0 都失败：网络大概率不可用，收手
        need_min_pages = pages < min_pages
        need_fill = len(fresh) < count and pages < RANDOM_PAGE_BUDGET
        if not (need_min_pages or need_fill):
            break
        pending = [t for t in types if t not in exhausted]
        if not pending:
            break
        subject_type = rng.choice(pending)

        if subject_type not in type_total:
            offset = 0  # 该类型第一页：用首页学习榜单总量
        else:
            total = type_total[subject_type]
            cap = type_cap.get(subject_type, total - limit)
            max_offset = max(0, min(total - limit, cap))
            slots = [
                i
                for i in range(max_offset // limit + 1)
                if i * limit not in used_offsets[subject_type]
            ]
            if not slots:
                exhausted.add(subject_type)
                continue
            offset = rng.choice(slots) * limit

        # 取页：随机偏移被拒（API 可能限制深翻页）时逐步减半回退到 0
        page = None
        worked_offset = None
        probe = offset
        while True:
            try:
                page = fetch_page(subject_type, probe, limit=limit)
                worked_offset = probe
                break
            except BangumiAPIError:
                if probe <= 0:
                    break
                probe //= 2
        if page is None:
            consecutive_failures += 1
            continue
        consecutive_failures = 0
        pages += 1
        used_offsets[subject_type].add(worked_offset)
        if worked_offset < offset:
            # 深偏移被拒、回退后才成功：记住该类型实际可用的偏移上限
            type_cap[subject_type] = worked_offset

        if page.total is not None:
            known = type_total.get(subject_type)
            if known is None or page.total > known:
                type_total[subject_type] = page.total
            if page.total <= limit:
                exhausted.add(subject_type)  # 榜单总量不足一页：只有这一页
            if rank_total is None or page.total > rank_total:
                rank_total = page.total
        elif subject_type not in type_total:
            # 响应缺 total 且此前没学到过：无法随机偏移，该类型只贡献这一页
            exhausted.add(subject_type)

        for sid in page.ids:
            if sid in existing:
                duplicates += 1
            elif sid not in seen:
                seen.add(sid)
                fresh.append(sid)
        nsfw_skipped += page.nsfw_skipped
        if pages < RANDOM_PAGE_BUDGET:
            sleep(RANK_SLEEP)

    if not fresh:
        return _empty_result()

    n = min(count, len(fresh))
    sampled = rng.sample(fresh, n)
    summary = run_import(
        sampled,
        session_factory=session_factory,
        apply=apply,
        proxy=proxy,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        base_url=base_url,
        fetch=fetch_subject,
        cover_downloader=cover_downloader,
        sleep=sleep,
    )
    return RandomImportResult(
        requested=count,
        sampled=sampled,
        summary=summary,
        pages=pages,
        candidates=len(fresh),
        duplicates=duplicates,
        nsfw_skipped=nsfw_skipped,
        rank_total=rank_total,
    )


# ---------- 数据映射（纯函数，便于单独测试） ----------


def _tag_candidates(payload: dict) -> list[tuple[int, str]]:
    """Bangumi tags -> [(标注数, 标签名)]，按标注数降序、同数按名称排序。

    count 缺失或非整数按 0 处理（排在后面），不因为个别标签缺计数就整体失败；
    tags 缺失 / 不是列表 / 没有合法项时返回空列表。
    """
    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, list):
        return []
    candidates: list[tuple[int, str]] = []
    for item in raw_tags:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name or len(name) > crud.TAG_NAME_MAX_LENGTH:
            continue
        count = item.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            count = 0
        candidates.append((count, name))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    return candidates


def extract_tags(
    payload: dict, limit: int = MAX_TAGS_PER_SUBJECT, exclude: set[str] | None = None
) -> list[str]:
    """从 Bangumi 条目 JSON 提取**题材**标签（经 app.tagging 归一）。

    规则：
    - 按标注数（count）从多到少过 ``is_genre_candidate``，只留「题材候选」：
      年份、公司、声优、媒介、平台（TV / PC / 剧场版 / 2011年10月…）在入库前
      就被丢掉，不占题材名额——批量导入链路与站内清洗同一套口径，
      否则标签云会被媒介标签霸榜（线上曾点 `TV` 出 2974 条）；
    - 放宽原 ``classify_tag`` 严格白名单：未进白名单但明显是主题的词也保留
      （解决「很多作品标签不够」），但作品信息（公司 / 年份 / 季度）与已知
      噪音（媒介 / 平台 / 地区 / 元标签）仍坚决排除；
    - ``exclude``：显式排除一组词（如本作真实的声优 / 主创名），避免作者 / 声优
      误入标签云；Bangumi 的 ``tags`` 字段本不含人物名，导入路径可不传；
    - 同义词归一到规范名（SF -> 科幻、癒し -> 治愈），标签云不因写法分裂；
      未命中规范名的真实主题词保留原名；
    - 按规范名去重（保序）、超长名丢弃（不截断，截断会产生语义错误的标签）；
    - tags 缺失 / 不是列表 / 没有题材候选时返回空列表——标签是可选增强，
      不因此跳过整个条目。
    """
    names: list[str] = []
    seen: set[str] = set()
    for _count, name in _tag_candidates(payload):
        if len(names) >= limit:
            break
        if not is_genre_candidate(name, exclude=exclude):
            continue
        canonical = canonical_genre(name) or name
        if canonical in seen:
            continue
        seen.add(canonical)
        names.append(canonical)
    return names


def extract_studios(payload: dict, limit: int = MAX_STUDIOS_PER_SUBJECT) -> list[str]:
    """从标签里识别制作公司（归一规范名，按标注数取前 limit 个）。

    公司不进 content_tags（那会污染标签筛选），结构化落到 content_studios，
    与收藏同步链路（crud.import_works_to_site）同一套口径。
    """
    names: list[str] = []
    for _count, name in _tag_candidates(payload):
        if len(names) >= limit:
            break
        kind, value = classify_tag(name)
        if kind != TAG_STUDIO or value in names:
            continue
        names.append(value)
    return names


def extract_year(payload: dict) -> int | None:
    """发行年：优先条目 date（YYYY-MM-DD），其次标签里的四位年份（取最早）。

    标签年份只是兜底——date 缺失的归档条目才用得上。
    """
    year = year_from_date(str(payload.get("date") or ""))
    if year:
        return year
    years = [year_of_tag for year_of_tag in _tag_years(payload)]
    return min(years) if years else None


def _tag_years(payload: dict) -> list[int]:
    out: list[int] = []
    for _count, name in _tag_candidates(payload):
        kind, value = classify_tag(name)
        if kind == TAG_YEAR and value:
            out.append(int(value))
    return out


# 书籍 infobox 里派生标签的键（文库 / 丛书 / 厂牌）：出版社已由
# app/entity_links 收进关联实体，文库没有结构化位置，作为标签补进轻小说区筛选。
BOOK_TAG_INFOBOX_KEYS: tuple[str, ...] = (
    "文库", "文庫", "レーベル", "label", "丛书", "叢書", "imprint",
)
_BOOK_TAG_KEY_LOOKUP = {
    key.casefold(): key for key in BOOK_TAG_INFOBOX_KEYS
}


def extract_book_tags(payload: dict, limit: int = MAX_BOOK_TAGS_PER_SUBJECT) -> list[str]:
    """书籍条目从 infobox 派生标签（文库 / 丛书，按出现顺序取前 limit 个）。

    为什么单独一条路（2026-10-19）：轻小说区此前的题材标签几乎全是「轻小说」
    「小说」这类载体词（归一后属噪音被丢弃），筛选栏形同虚设。文库 / 丛书是
    轻小说最自然的浏览面（電撃文庫、角川スニーカー文庫…），Bangumi 把它放在
    infobox 里，而 entity_links 明确不收文库（那不是「系列」语义），所以在这里
    补成标签。不过 classify_tag——这些是书籍侧的结构化信号，按 key 白名单放行。
    """
    raw = payload.get("infobox")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if key.strip().casefold() not in _BOOK_TAG_KEY_LOOKUP:
            continue
        for value in _infobox_values(item.get("value")):
            if value and value not in out and len(value) <= crud.TAG_NAME_MAX_LENGTH:
                out.append(value)
        if len(out) >= limit:
            break
    return out[:limit]


def _infobox_values(value) -> list[str]:
    """infobox 值 -> 字符串列表（字符串原样，列表逐项转字符串）。

    API 结构里「别名」的值是 ``[{"v": "EVA"}, ...]``（每项一个别名），
    要取 dict 的 ``v`` 键，不能 str(dict) 整个序列化进去。
    """
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict) and "v" in item:
                text = str(item["v"]).strip()
            else:
                text = str(item).strip()
            if text:
                out.append(text)
        return out
    return []


MAX_ALIASES_PER_SUBJECT = 20  # 别名只服务搜索召回，20 个足够覆盖正常条目


def extract_alias_names(payload: dict, title: str, title_original: str | None) -> list[str]:
    """从 infobox「别名」键提取搜索用别名，过滤垃圾值。

    过滤规则（确定性，供增量入库与本机/服务器回填脚本共用，防口径漂移）：
    - 丢弃 ≤1 字符与纯数字（infobox 实测混着「第四个」这类 wiki 噪音，
      太短的别名在 LIKE 检索里只会制造误命中）；
    - 丢弃与 title / title_original 重复的值；
    - 顺序去重，超过 MAX_ALIASES_PER_SUBJECT 截断。
    解析失败/无「别名」键返回空列表，绝不抛异常——别名是增强不是关键路径。
    """
    infobox = payload.get("infobox")
    if not isinstance(infobox, list):
        return []
    raw: list[str] = []
    for item in infobox:
        if isinstance(item, dict) and item.get("key") == "别名":
            raw.extend(_infobox_values(item.get("value")))
    out: list[str] = []
    seen: set[str] = set()
    reserved = {title.strip(), (title_original or "").strip()}
    for name in raw:
        name = name.strip()
        if len(name) <= 1 or name.isdigit():
            continue
        if name in reserved or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_ALIASES_PER_SUBJECT:
            break
    return out


def map_subject(payload: dict) -> MappedSubject:
    """把 Bangumi API 的条目 JSON 映射为可入库字段。

    规则：
    - 标题优先 name_cn，为空时回退 name；两者都为空则跳过；
    - summary 映射到摘要；为空则跳过（summary 是必填字段）；
    - rating 只取 rating.score 且必须 > 0，取不到就跳过（rating 必填，不伪造）；
    - type=2 -> anime，type=4 -> game；type=1 书籍按 platform 拆分（1001/"漫画" ->
      comic，1002/"小说" -> novel 且经 ``classify_book`` 给出 book_kind 标注；
      非日本语种的小说直接跳过）；音乐（3）与三次元/真人（5、6）跳过；
    - 收录口径（2026-10-21）：小说只收轻小说（文学不收）；游戏只收 galge
      （tags 判定）或带 ``_site_related_ip`` 标记（选品端确认有动画/漫画
      相关）的条目；其余游戏跳过；
    - tags 只留题材（``extract_tags``，经 app.tagging 归一）；公司进 studios、
      年份进 year，各自有结构化位置；书籍条目另从 infobox 派生文库标签
      （``extract_book_tags``）。
    无法映射时抛 SubjectMappingError，异常信息即跳过原因。
    """
    bangumi_id = payload.get("id")
    if not isinstance(bangumi_id, int) or bangumi_id <= 0:
        raise SubjectMappingError(f"缺少有效的条目 id（实际值：{payload.get('id')!r}）")

    content_type, book_kind = resolve_book(payload)
    if content_type is None:
        type_name = TYPE_NAMES.get(payload.get("type"), f"未知类型 {payload.get('type')!r}")
        raise SubjectMappingError(
            f"暂不支持导入「{type_name}」类条目（本站只收录动画/游戏/小说/漫画；"
            "书籍区画集与非日本语种的小说也不收录）"
        )

    # ---------- 收录口径（2026-10-21 用户定稿） ----------
    # 小说只收轻小说：文学（日本普通小说）与判不出文库信号的边界件不收。
    # 此前口径是「文学留站内待人工复核」（classify_book 的注释里那套取舍）；
    # 现改为入库闸口直接拦下，classify_book 本身语义不变——存量打标
    # （scripts/mark_book_kinds.py）与其它调用方不受影响。
    if content_type == "novel" and book_kind != BOOK_KIND_LIGHT_NOVEL:
        raise SubjectMappingError(
            "文学类小说不收录（本站小说区只收轻小说）"
        )
    # 游戏只收 galge（tags 可判），或选品端查过镜像关联条目、确认「有动画 /
    # 漫画相关」并打上 payload["_site_related_ip"]=True 标记的条目
    # （scripts/select_from_archive.apply_ingest_policy 负责）。标记缺失的
    # 非 galge 游戏（随机导入、手动 run_sync 等）一律拦下。
    if content_type == "game" and not (
        is_galge_payload(payload) or payload.get("_site_related_ip") is True
    ):
        raise SubjectMappingError(
            "普通游戏不收录（本站游戏区只收 galge 或有动画/漫画相关的游戏）"
        )

    title = (payload.get("name_cn") or "").strip() or (payload.get("name") or "").strip()
    if not title:
        raise SubjectMappingError("缺少可用标题（name_cn 与 name 均为空）")
    if len(title) > TITLE_MAX_LENGTH:
        raise SubjectMappingError(f"标题超过 {TITLE_MAX_LENGTH} 字符，无法安全存入 title 字段")
    # 日文原名：title 走了 name_cn（即两者不同）时才单独存，避免冗余重复。
    name = (payload.get("name") or "").strip()
    title_original = name if name and name != title else None

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
        type=content_type,
        summary=summary,
        rating=float(score),
        source_url=SOURCE_URL_TEMPLATE.format(subject_id=bangumi_id),
        tags=_subject_tags(payload, content_type, exclude=_build_tag_exclude(payload)),
        studios=extract_studios(payload),
        year=extract_year(payload),
        image_url=extract_cover_url(payload),
        book_kind=book_kind,
        title_original=title_original,
        aliases=extract_alias_names(payload, title, title_original),
    )


def _subject_tags(
    payload: dict, content_type: str, exclude: set[str] | None = None
) -> list[str]:
    """题材标签：动画/漫画/游戏走 Bangumi 标签归一；书籍另补 infobox 文库标签。

    书籍的 Bangumi 标签常被「轻小说」「小说」这类载体词占满名额（归一后属
    噪音），所以文库 / 丛书标签对小说区是关键补充，放在归一标签之后。
    ``exclude`` 透传给 ``extract_tags``（声优 / 主创名兜底排除）。
    """
    tags = extract_tags(payload, exclude=exclude)
    if content_type != "novel":
        return tags
    for name in extract_book_tags(payload):
        if name not in tags:
            tags.append(name)
        if len(tags) >= MAX_TAGS_PER_SUBJECT:
            break
    return tags[:MAX_TAGS_PER_SUBJECT]


def _build_tag_exclude(payload: dict) -> set[str]:
    """构造本作应排除的「作品信息」词：infobox 主创/公司名 + 自身标题/别名。

    Bangumi 的 tags 字段里常混有导演 / 作曲 / 制作公司 / 作品自身译名，这些属
    作品信息而非题材，应排掉（与「作者声优公司不进标签」一致）。persons/characters
    接口在镜像上不一定返回，这里以 /v0/subjects 内自带的 infobox 为准。
    """
    excluded: set[str] = set()
    subject = payload if "infobox" in payload else payload.get("subject") or payload
    info = infobox_map(subject)
    for role in (
        "导演", "系列构成", "音乐", "人物设定", "原作", "脚本",
        "美术监督", "音响监督", "总作画监督", "作画监督", "动画制作",
        "企画", "制作",
    ):
        for raw in info.get(role, []):
            name = _clean_name(str(raw))
            if name:
                excluded.add(name)
    for raw in (subject.get("name"), subject.get("name_cn")):
        if raw:
            excluded.add(str(raw).strip())
    for raw in info.get("别名", []):
        name = _clean_name(str(raw))
        if name:
            excluded.add(name)
    return excluded


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


def _map_and_store(db, payload: dict, *, apply: bool, cover_downloader=False) -> SubjectOutcome:
    """对已成功获取的条目做映射、查重、入库，返回处理结果。

    - bangumi_id 已存在 -> duplicate，直接跳过不覆盖（含标签）；
    - 预览模式 -> preview，不写库、不下图；
    - 正式模式写入失败（含唯一索引冲突、数据库错误）会回滚并归入对应结果，
      不中断其余条目的导入；
    - 正式入库成功后，**题材**标签经 ``crud.replace_content_tags`` 替换式写入
      （与站内筛选同一套表，不另写标签逻辑；年份/公司/声优/媒介已在
      ``extract_tags`` 阶段分流：公司进 content_studios、年份进 contents.year）；
    - 正式入库成功后，信息卡（infobox）经 ``app.entity_links`` 白名单识别
      作者/公司关系，``crud.replace_content_entity_links`` 替换式写入；
      没有可用键时静默跳过，识别失败（数据库错误）归入 failed；
    - 正式入库成功后，若传入了 cover_downloader（callable）且条目有远端封面，
      下载封面并回写 ``Content.image_url``；下载失败只在 reason 里注明，
      不影响入库结果本身。
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
            year=subject.year,
            book_kind=subject.book_kind,
            title_original=subject.title_original,
            aliases=subject.aliases,
        )
        db.add(content)
        db.commit()
        # 标签与作品一起落库：替换式写入，重复导入不会堆叠关联行
        crud.replace_content_tags(db, content.id, subject.tags)
        # 公司单独成表（与收藏同步链路同一套口径）：标签里的公司名不进标签云
        if subject.studios:
            crud.replace_content_studios(db, content.id, subject.studios)

        # 作者/公司关系与作品一起落库：infobox 白名单识别，替换式写入
        entity_note = _link_entities(db, content.id, payload)

        # 封面本地化：失败不阻塞入库，仅在结果里注明
        cover_note = _localize_cover(db, content, subject, cover_downloader)
        return SubjectOutcome(
            bangumi_id=subject.bangumi_id,
            status="imported",
            title=subject.title,
            reason=f"已写入 contents.id={content.id}"
            + (f"，标签 {len(subject.tags)} 个" if subject.tags else "")
            + entity_note
            + cover_note,
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


def _link_entities(db, content_id: int, payload: dict) -> str:
    """识别并写入作者/公司关系，返回追加到导入结果里的说明。

    信息卡里没有可用键（绝大多数书籍/游戏条目）时返回空串；识别算法本身
    防御性解析（app/entity_links.recognize 不抛），数据库错误向上传播，
    由 _map_and_store 的统一 handler 归入 failed——与标签写入同一语义。
    """
    recognition = entity_links.recognize(content_id, payload)
    if not recognition.has_data():
        return ""
    entity_links.apply_recognition(db, recognition)
    return f"，{recognition.describe()}"


def _localize_cover(db, content: Content, subject: MappedSubject, cover_downloader) -> str:
    """下载并回写封面，返回追加到导入结果里的说明（没有封面时为空串）。"""
    if not subject.image_url:
        return ""
    if cover_downloader is False or cover_downloader is None:
        return "，封面未本地化（本次运行关闭了封面下载）"
    if not callable(cover_downloader):
        return "，封面未本地化（未提供下载器）"
    try:
        local_url = cover_downloader(subject.image_url, subject.bangumi_id)
    except CoverDownloadError as exc:
        return f"，封面下载失败（{exc}）"
    if not local_url:
        return "，封面未本地化"
    content.image_url = local_url
    db.commit()
    return f"，封面已本地化（{local_url}）"


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
    cover_downloader=None,
) -> ImportSummary:
    """导入主流程：逐个获取 -> 映射 -> 查重/入库，返回汇总。

    参数说明：
    - session_factory：产生 SQLAlchemy Session 的工厂（如 SessionLocal），
      本函数负责打开与关闭会话；
    - fetch：可选的 fetch(subject_id) -> dict 注入点，测试用它替代真实 API；
      不传时按 proxy/timeout 等参数构造真实请求器；
    - cover_downloader：封面下载器 download(remote_url, bangumi_id) -> 站内地址。
      默认 None 表示按 settings.cover_download_enabled 自动构造；
      显式传 False 可强制不下载（CLI --no-cover 即走此路）。
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

    if cover_downloader is None:
        if settings.cover_download_enabled:
            cover_downloader = _make_cover_downloader(
                opener=build_opener(proxy),
                timeout=timeout,
                retries=retries,
                user_agent=user_agent,
                sleep=sleep,
            )
        else:
            cover_downloader = False

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
            outcomes.append(
                _map_and_store(db, payload, apply=apply, cover_downloader=cover_downloader)
            )
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
        "--no-cover",
        action="store_true",
        help="正式入库时跳过封面下载（只写数据，不落图）",
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
    if args.apply:
        cover_state = (
            "关闭（--no-cover）"
            if args.no_cover
            else ("开启" if settings.cover_download_enabled else "关闭（COVER_DOWNLOAD_ENABLED=false）")
        )
        print(f"封面本地化：{cover_state}（目录 {settings.cover_storage_dir}）")

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
            cover_downloader=False if args.no_cover else None,
        )
    finally:
        engine.dispose()

    for outcome in summary.outcomes:
        print(outcome.describe())
    print(summary.describe())
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
