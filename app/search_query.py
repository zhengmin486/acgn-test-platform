"""搜索查询理解层（语义归纳，2026-10-19）。

把用户的自然语言搜索词拆解成站内结构化检索条件，让「治愈日常 动画 2019年
第二季」这类查询能落到 标签 AND / 类型 / 年份 / 季数变体 上——复用
``app.tagging`` 的题材同义词表（GENRE_TAGS / GENRE_ALIASES），不新造口径。

设计原则（与推荐口味词展开同一哲学，见 tagging.expand_genre_names）：
- **确定性规则**，可单测、可回滚；不引入模型与外部依赖；
- **守卫优先**：只在一整个 token 能被明确归类的场合才消费它——「游戏王」
  不能因为含「游戏」就被拆成类型筛选，标题类 token 一律保持原样走关键词；
- 匹配语义只放宽不收紧：识别失败的 token 原样保留为关键词，最差情况等于
  旧版纯 LIKE 行为。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.tagging import GENRE_ALIASES, GENRE_TAGS, canonical_genre

# ---------- 类型词（token 级精确匹配，防「游戏王」误伤） ----------

TYPE_WORD_TO_CODE: dict[str, str] = {
    "动画": "anime",
    "动漫": "anime",
    "番剧": "anime",
    "番": "anime",
    "漫画": "comic",
    "游戏": "game",
    "轻小说": "novel",
    "小说": "novel",
}

# ---------- 题材词 ----------

# 表面写法 -> 站内规范题材名：规范名自身 + 全部别名（与 tagging 同一张表）。
_GENRE_SURFACE: dict[str, str] = {}
for _g in GENRE_TAGS:
    _GENRE_SURFACE[_g] = _g
for _canonical, _aliases in GENRE_ALIASES.items():
    for _alias in (_canonical, *_aliases):
        _alias = _alias.strip()
        if _alias and _alias not in _GENRE_SURFACE:
            _GENRE_SURFACE[_alias] = _canonical

# 贪心分词用：按长度降序（「魔法少女」优先于「魔法」）。
_GENRE_SURFACE_BY_LEN: tuple[str, ...] = tuple(
    sorted(_GENRE_SURFACE, key=len, reverse=True)
)


def _segment_genres(token: str) -> list[str]:
    """把一个 token 整段贪心切成题材词；切不干净（有剩余）返回空。

    整段消费是硬约束：「治愈日常」→治愈+日常；「校园恋爱」→校园+恋爱；
    「轻喜剧」含未消费的「轻」→ 不算题材词，保持关键词原样——宁可漏识别，
    不可把标题误拆成筛选条件。
    """
    parts: list[str] = []
    rest = token
    while rest:
        for surface in _GENRE_SURFACE_BY_LEN:
            if rest.startswith(surface):
                parts.append(_GENRE_SURFACE[surface])
                rest = rest[len(surface):]
                break
        else:
            return []
    return parts


# ---------- 季数等价类 ----------

_CN_DIGIT: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number(text: str) -> int | None:
    """中文数字 / 阿拉伯数字（含全角）-> 整数；失败返回 None。"""
    text = unicodedata.normalize("NFKC", (text or "")).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    for ch in text:
        if ch == "十":
            total = (total or 1) * 10
        elif ch in _CN_DIGIT:
            total += _CN_DIGIT[ch]
        else:
            return None
    return total or None


def cn_number_string(n: int) -> str:
    """整数 -> 中文数字串（2->二, 12->十二），供季数变体生成。"""
    if n < 0:
        return str(n)
    if n < 10:
        return {v: k for k, v in _CN_DIGIT.items()}.get(n, str(n)).replace("两", "二")
    if n == 10:
        return "十"
    if n < 20:
        return "十" + cn_number_string(n - 10)
    tens, ones = divmod(n, 10)
    return cn_number_string(tens) + "十" + (cn_number_string(ones) if ones else "")


# Unicode 罗马数字 -> 整数（搜索词与标题两侧都可能出现）。
_ROMAN_TOKENS: dict[str, int] = {
    "Ⅻ": 12, "Ⅺ": 11, "Ⅹ": 10, "Ⅸ": 9, "Ⅷ": 8, "Ⅶ": 7, "Ⅵ": 6,
    "Ⅴ": 5, "Ⅳ": 4, "Ⅲ": 3, "Ⅱ": 2,
    "XII": 12, "XI": 11, "X": 10, "IX": 9, "VIII": 8, "VII": 7, "VI": 6,
    "V": 5, "IV": 4, "III": 3, "II": 2,
}
# 整数 -> (Unicode 罗马形态, ASCII 形态)，只收 ≥2（「I」与字母 I 歧义太大）。
_ROMAN_OF: dict[int, tuple[str, str]] = {}
for _ascii_form, _n in _ROMAN_TOKENS.items():
    if _n < 2:
        continue
    _unicode_form = next(
        (u for u, m in {
            "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8,
            "Ⅸ": 9, "Ⅹ": 10, "Ⅺ": 11, "Ⅻ": 12,
        }.items() if m == _n),
        _ascii_form,
    )
    _ROMAN_OF[_n] = (_unicode_form, _ascii_form)

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    return f"{n}{_ORDINAL_SUFFIX.get(n, 'th')}"


# 第X季/第X期：X 允许阿拉伯数字（含全角）与中文数字。
_SEASON_CN_RE = re.compile(r"第\s*([0-9０-９一二三四五六七八九十两]+)\s*[季期]")
# 独立 token 形态：2nd / 3rd（大小写不敏感）；罗马数字走 _ROMAN_TOKENS 精确表。
_ORDINAL_TOKEN_RE = re.compile(r"^([2-9]\d?)(?:ST|ND|RD|TH)$")


def parse_season_suffix(text: str) -> tuple[str, int | None]:
    """从查询里剥出季数：返回 (剩余主体, 季数或 None)。

    识别形态：第2季 / 第二期 / 第 十二 季（含空格）、独立 token「II」「Ⅱ」
    「2nd」「3rd season」。罗马/序数形态只认 ≥2（「I」「1st」歧义太大）；
    剥完主体为空时放弃拆分（搜「第二季」就整串走关键词），避免空关键词全表扫。
    """
    text = (text or "").strip()
    if not text:
        return "", None

    match = _SEASON_CN_RE.search(text)
    if match:
        number = _cn_number(match.group(1))
        if number:
            base = (text[: match.start()] + " " + text[match.end():]).strip()
            base = _clean_fragments(base)
            if base:
                return base, number
            return text, None

    tokens = text.split()
    for index in range(len(tokens) - 1, -1, -1):
        upper = tokens[index].upper()
        number = None
        m = _ORDINAL_TOKEN_RE.match(upper)
        if m:
            number = int(m.group(1))
        elif upper in _ROMAN_TOKENS and _ROMAN_TOKENS[upper] >= 2:
            number = _ROMAN_TOKENS[upper]
        if number:
            base_tokens = tokens[:index] + tokens[index + 1:]
            base = _clean_fragments(" ".join(base_tokens))
            if base:
                return base, number
            return text, None
    return text, None


def season_variants(n: int) -> list[str]:
    """季数 n 的全部常见写法（供 SQL OR-LIKE 标题匹配）。

    第2季/第2期/第二季/第二期；n≥2 追加罗马数字（Unicode+ASCII，标题里的
    「II」「Ⅱ」都是独立短词，LIKE 子串误命中风险可接受）；再加「2nd」类
    序数写法（覆盖「2nd season」标题）。
    """
    n = max(1, n)
    cn = cn_number_string(n)
    variants = [f"第{n}季", f"第{n}期", f"第{cn}季", f"第{cn}期"]
    if n in _ROMAN_OF:
        unicode_form, ascii_form = _ROMAN_OF[n]
        variants.extend({unicode_form, ascii_form})
    variants.append(_ordinal(n))
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------- 年份 ----------

_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)\s*年?")


# ---------- 查询解析 ----------


@dataclass
class ParsedQuery:
    """一次查询的结构化拆解结果。

    keyword 是剥离类型/题材/年份/季数后剩余的关键词（可能为空，表示纯筛选
    浏览）；genres 为站内规范题材名（AND 语义，与 ?tags= 同口径）。
    """

    original: str = ""
    keyword: str = ""
    content_type: str | None = None
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    season_number: int | None = None
    season_base: str | None = None

    @property
    def has_intent(self) -> bool:
        """是否识别出任何结构化意图（供页面提示「已按…理解」用）。"""
        return bool(
            self.content_type or self.genres or self.year is not None
            or self.season_number
        )


def _clean_fragments(text: str) -> str:
    """剥掉拆解残留的虚词「的」与多余空白。

    只按整个虚词剥（首尾各剥到没有为止），不用字符集 strip——「太多了」
    的「了」是词的一部分，字符集剥离会误删。
    """
    text = text.strip()
    while text.startswith("的"):
        text = text[1:].strip()
    while text.endswith("的"):
        text = text[:-1].strip()
    return text


def parse_query(q: str) -> ParsedQuery:
    """解析搜索词：类型/题材（token 级）+ 年份 + 季数 + 剩余关键词。

    流程：全角归一 → 年份提取 → 逐 token 分类（类型词优先于题材词，题材
    要求整段贪心切分干净）→ 季数拆分 → 清理虚词。任何一步识别失败都把
    原文留给关键词，绝不收紧匹配。
    """
    text = unicodedata.normalize("NFKC", (q or "")).strip()
    parsed = ParsedQuery(original=(q or "").strip())
    if not text:
        return parsed

    # 1. 年份
    year_match = _YEAR_RE.search(text)
    if year_match:
        parsed.year = int(year_match.group(1))
        text = _clean_fragments(text[: year_match.start()] + " " + text[year_match.end():])

    # 2. 逐 token：类型词 / 题材词 / 关键词
    keyword_tokens: list[str] = []
    for token in text.split():
        token = token.strip()
        if not token:
            continue
        if token in TYPE_WORD_TO_CODE:
            if parsed.content_type is None:
                parsed.content_type = TYPE_WORD_TO_CODE[token]
            continue
        # 「治愈动画」「搞笑番」：题材头 + 类型词尾的复合 token——头能整段
        # 切成题材才算命中（「游戏王」不以类型词结尾，天然不受影响）
        compound = False
        for word in sorted(TYPE_WORD_TO_CODE, key=len, reverse=True):
            if token.endswith(word) and len(token) > len(word):
                head_genres = _segment_genres(token[: -len(word)])
                if head_genres:
                    for g in head_genres:
                        if g not in parsed.genres:
                            parsed.genres.append(g)
                    if parsed.content_type is None:
                        parsed.content_type = TYPE_WORD_TO_CODE[word]
                    compound = True
                    break
        if compound:
            continue
        # 「治愈系」「日常番」：剥俗称后缀再试题材；切不干净就保持关键词
        stripped = token[:-1] if token.endswith(("系", "番")) and len(token) > 2 else token
        genres = _segment_genres(stripped) if stripped else []
        if genres:
            for g in genres:
                if g not in parsed.genres:
                    parsed.genres.append(g)
            continue
        keyword_tokens.append(token)

    # 3. 季数拆分（对剩余关键词）
    keyword = " ".join(keyword_tokens).strip()
    if keyword:
        base, number = parse_season_suffix(keyword)
        if number:
            parsed.season_number = number
            parsed.season_base = base
            keyword = base

    parsed.keyword = _clean_fragments(keyword)
    return parsed
