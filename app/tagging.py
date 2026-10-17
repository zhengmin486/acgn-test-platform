"""Bangumi 标签归一：区分题材 / 制作公司 / 年份，丢掉声优、载体等噪音。

为什么需要：
- Bangumi 的标签是用户自由打的，一部动画的标签里除了「日常」「治愈」，
  还混着 `2011`（年份）、`A-1 Pictures`（公司）、`花泽香菜`（声优）、
  `TV`（媒介）、`漫画原作`（载体）；
- 站内 `content_tags` 只应该放**题材**：标签云和「按标签筛选」都依赖它，
  混进年份/公司会让筛选结果失真；
- 但年份与公司本身有价值，不能直接丢：年份写 `contents.year`，
  公司写 `content_studios`，各自有结构化位置。

调用方：`crud.import_works_to_site`（收藏同步入库）、`scripts/clean_content_tags.py`
（历史数据清洗）、`scripts/import_bangumi.py`（Bangumi 直接导入）、`app/persona.py`
（画像）。
"""

from __future__ import annotations

import re

TAG_GENRE = "genre"
TAG_STUDIO = "studio"
TAG_YEAR = "year"
TAG_NOISE = "noise"

# 题材簇：用标签命中数归到一个人格母题
TAG_CLUSTERS = {
    "治愈": {"治愈", "日常", "温馨", "芳文社", "空气系", "萌", "萌系"},
    "热血": {"热血", "战斗", "少年", "王道", "燃", "格斗"},
    "悬疑": {"悬疑", "推理", "惊悚", "犯罪", "心理", "悬疑推理"},
    "科幻": {"科幻", "机战", "机甲", "赛博朋克", "太空", "SF"},
    "恋爱": {"恋爱", "爱情", "党争", "后宫", "修罗场", "青春", "校园"},
    "奇幻": {"奇幻", "魔法", "异世界", "冒险", "神话", "剑与魔法"},
    "搞笑": {"搞笑", "喜剧", "沙雕", "吐槽", "欢乐"},
    "音乐": {"音乐", "乐队", "偶像", "Live", "歌唱"},
}

# 题材标签白名单
GENRE_TAGS = set()
for _names in TAG_CLUSTERS.values():
    GENRE_TAGS |= _names
GENRE_TAGS |= {
    "后宫", "百合", "傲娇", "修罗场", "党争", "扭曲", "猎奇", "校园", "青春",
    "耽美", "乙女", "机战", "运动", "偶像", "悬疑", "推理", "恐怖", "治愈",
    "日常", "搞笑", "科幻", "奇幻", "恋爱", "战斗", "热血", "冒险", "异世界",
    "音乐", "乐队", "萌", "萌系", "空气系", "群像", "单元剧", "职场", "美食",
    "历史", "战争", "犯罪", "心理", "超能力", "魔法少女", "萝卜", "特摄",
    "致郁", "沉重", "黑深残",
}

GENRE_KEYWORDS = (
    "治愈", "日常", "热血", "悬疑", "科幻", "恋爱", "奇幻", "搞笑",
    "音乐", "后宫", "百合", "机战", "运动", "偶像", "校园", "青春",
    "致郁", "猎奇", "扭曲",
)

# 制作公司别名（Bangumi 标签里常见写法）；键是展示用规范名。
STUDIO_ALIASES = {
    "京都动画": {"京都动画", "京阿尼", "Kyoto Animation", "京都アニメーション"},
    "MAPPA": {"MAPPA"},
    "ufotable": {"ufotable", "飞碟社"},
    "A-1 Pictures": {"A-1 Pictures", "A1 Pictures"},
    "J.C.STAFF": {"J.C.STAFF", "JCSTAFF"},
    "动画工房": {"动画工房", "動画工房"},
    "Production I.G": {"Production.I.G", "Production I.G", "ProductionIG"},
    "BONES": {"BONES", "骨头社"},
    "SUNRISE": {"SUNRISE", "日升"},
    "P.A.WORKS": {"P.A.WORKS", "PAWORKS"},
    "SHAFT": {"SHAFT"},
    "TRIGGER": {"TRIGGER"},
}

_STUDIO_CANONICAL = {
    alias.casefold(): canonical
    for canonical, aliases in STUDIO_ALIASES.items()
    for alias in aliases
}

# 公司名的常见后缀：命中即视为公司（但「动画」「アニメ」本身是媒介标签，排除）。
_STUDIO_SUFFIXES = (
    "pictures",
    "animation",
    "studio",
    "works",
    "アニメーション",
    "スタジオ",
    "工房",
)

# 明确属于噪音的标签：媒介、载体、地区、放送形态。
NOISE_TAGS = {
    "tv", "剧场版", "映画", "ova", "oad", "web", "动画", "アニメ", "漫画",
    "轻小说", "小说", "游戏", "galgame", "日本", "中国", "美国", "韩国",
    "原创", "漫画原作", "小说原作", "游戏原作", "多媒体企划",
}

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_SUMMARY_YEAR_RE = re.compile(r"(19|20)\d{2}(?=\s*年)")


def is_genre_tag(tag: str) -> bool:
    """是否为题材类标签（画像侧口径）。"""
    if not tag:
        return False
    if _YEAR_RE.match(tag):
        return False
    if tag in GENRE_TAGS:
        return True
    return any(keyword in tag for keyword in GENRE_KEYWORDS)


def studio_name(tag: str) -> str | None:
    """标签若是制作公司，返回展示用规范名；否则 None。"""
    name = (tag or "").strip()
    if not name:
        return None
    canonical = _STUDIO_CANONICAL.get(name.casefold())
    if canonical:
        return canonical
    lowered = name.casefold()
    if len(name) > 3 and any(lowered.endswith(suffix) for suffix in _STUDIO_SUFFIXES):
        return name
    return None


def year_of(tag: str) -> int | None:
    """标签若是四位年份（19xx / 20xx），返回整数年份。"""
    name = (tag or "").strip()
    if _YEAR_RE.match(name):
        return int(name)
    return None


def classify_tag(tag: str) -> tuple[str, str]:
    """把单个标签归类：返回 (kind, 值)。

    kind 为 ``genre`` / ``studio`` / ``year`` / ``noise``；
    ``year`` 的值是四位数字符串，``studio`` 是规范名，其余原样（已 strip）。
    """
    name = (tag or "").strip()
    if not name:
        return TAG_NOISE, ""
    year = year_of(name)
    if year is not None:
        return TAG_YEAR, str(year)
    studio = studio_name(name)
    if studio:
        return TAG_STUDIO, studio
    if name.casefold() in NOISE_TAGS:
        return TAG_NOISE, name
    if is_genre_tag(name):
        return TAG_GENRE, name
    return TAG_NOISE, name


def split_tags(tags) -> tuple[list[str], list[str], int | None]:
    """把标签列表拆成 (题材, 公司, 年份)。

    - 题材与公司保序去重；年份取先出现的那个；
    - 噪音（声优 / 载体 / 平台）直接丢弃；
    - 年份如果同时有多个，取最早的一个（作品首播年更接近事实）。
    """
    genres: list[str] = []
    studios: list[str] = []
    years: list[int] = []
    for tag in tags or []:
        kind, value = classify_tag(str(tag))
        if kind == TAG_GENRE and value not in genres:
            genres.append(value)
        elif kind == TAG_STUDIO and value not in studios:
            studios.append(value)
        elif kind == TAG_YEAR and value:
            years.append(int(value))
    return genres, studios, (min(years) if years else None)


def year_from_date(date: str | None) -> int | None:
    """从 Bangumi 的 ``YYYY-MM-DD`` 取年份；取不到返回 None。"""
    text = (date or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        if 1900 <= year <= 2100:
            return year
    return None


def year_from_summary(summary: str | None) -> int | None:
    """从站内简介（``2006 年的动画，…``）里兜底解析年份。"""
    match = _SUMMARY_YEAR_RE.search(summary or "")
    return int(match.group(0)) if match else None
