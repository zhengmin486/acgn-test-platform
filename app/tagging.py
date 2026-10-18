"""Bangumi 标签归一：区分题材 / 制作公司 / 年份，丢掉声优、载体等噪音。

为什么需要：
- Bangumi 的标签是用户自由打的，一部动画的标签里除了「日常」「治愈」，
  还混着 `2011`（年份）、`A-1 Pictures`（公司）、`花泽香菜`（声优）、
  `TV`（媒介）、`PC`（平台）、`2011年10月`（季度）；
- 站内 `content_tags` 只应该放**题材**：标签云和「按标签筛选」都依赖它，
  混进年份/公司/媒介会让筛选结果失真（线上曾出现点 `TV` 出 2974 条、
  实际可见作品远少于此的虚高计数）；
- 但年份与公司本身有价值，不能直接丢：年份写 `contents.year`，
  公司写 `content_studios`，各自有结构化位置。

口径要点（2026-10-19 扩充）：
- 题材词表按「动画 / 漫画 / 游戏 / 书籍」四类内容统一扩充，并新增
  **同义词表**（SF→科幻、癒し→治愈、日常系→日常…），查找键做
  NFKC + casefold 归一（全角ＳＦ、ｶﾀｶﾅ半角写法都能命中）；
- 游戏类型词（RPG / ACT / AVG / Galgame / 开放世界…）算题材；
  平台词（PC / PS5 / Switch / 手机…）算噪音——本站没有平台维度，
  放进标签云只会把游戏区筛选变成平台筛选；
- 季度标签（`2011年10月`）算噪音：发行年已由 date / year 承载；
- 默认未知标签一律噪音（宁可少一个题材，也不让筛选失真）。

调用方：`crud.import_works_to_site`（收藏同步入库）、`scripts/clean_content_tags.py`
（历史数据清洗）、`scripts/import_bangumi.py`（Bangumi 直接导入与批量归档导入）、
`app/persona.py`（画像）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

TAG_GENRE = "genre"
TAG_STUDIO = "studio"
TAG_YEAR = "year"
TAG_NOISE = "noise"

# 题材簇：用标签命中数归到一个人格母题
TAG_CLUSTERS = {
    "治愈": {"治愈", "日常", "温馨", "芳文社", "空气系", "萌", "萌系", "美食", "旅行", "田园", "友情"},
    "热血": {"热血", "战斗", "少年", "王道", "燃", "格斗", "武侠", "竞技", "动作", "生存"},
    "悬疑": {"悬疑", "推理", "惊悚", "犯罪", "心理", "悬疑推理", "智斗", "权谋", "复仇", "剧情"},
    "科幻": {
        "科幻", "机战", "机甲", "赛博朋克", "太空", "SF", "末日", "丧尸",
        "克苏鲁", "蒸汽朋克", "机器人", "时间旅行",
    },
    "恋爱": {"恋爱", "爱情", "党争", "后宫", "修罗场", "青春", "校园", "纯爱", "青梅竹马", "同居", "婚姻"},
    "奇幻": {
        "奇幻", "魔法", "异世界", "冒险", "神话", "剑与魔法", "神怪", "妖怪",
        "吸血鬼", "恶魔", "天使", "龙", "转生",
    },
    "搞笑": {"搞笑", "喜剧", "沙雕", "吐槽", "欢乐"},
    "音乐": {"音乐", "乐队", "偶像", "Live", "歌唱"},
}

# 题材标签白名单：动画/漫画/书籍通用题材 + 书籍常见标签。
GENRE_TAGS = set()
for _names in TAG_CLUSTERS.values():
    GENRE_TAGS |= _names
GENRE_TAGS |= {
    # 关系 / 角色属性
    "后宫", "百合", "傲娇", "修罗场", "党争", "扭曲", "猎奇", "校园", "青春",
    "耽美", "乙女", "三角关系", "兄妹", "姐妹", "主仆", "人外", "兽耳",
    # 氛围 / 风格
    "致郁", "沉重", "黑深残", "单元剧", "群像", "职场", "童话", "传记",
    "旅行", "田园", "治愈系",
    # 题材母题（动画/书籍通用）
    "历史", "战争", "犯罪", "超能力", "魔法少女", "萝卜", "特摄", "末日",
    "丧尸", "转生", "复仇", "武侠", "神怪", "智斗", "权谋", "剧情", "生存",
    "海盗", "西部", "战国", "三国", "航海", "探险", "遗迹", "秘境",
    "吸血鬼", "妖怪", "恶魔", "天使", "龙", "克苏鲁", "蒸汽朋克",
    "赛博朋克", "机器人", "时间旅行", "虚拟现实", "太空",
    "青梅竹马", "同居", "婚姻", "纯爱",
    "美食", "料理", "音乐", "歌唱",
    # 运动 / 竞技
    "运动", "竞技", "足球", "篮球", "网球", "排球", "棒球", "乒乓球",
    "将棋", "围棋", "拳击", "赛车", "电竞", "游泳",
    # 游戏类型（平台词见 NOISE_TAGS，本站无平台维度）
    "RPG", "ARPG", "JRPG", "ACT", "AVG", "ADV", "SLG", "FTG", "STG",
    "RTS", "FPS", "TPS", "MOBA", "Roguelike", "开放世界",
    "沙盒", "塔防", "卡牌", "解谜", "益智", "棋牌", "竞速", "音游",
    "音乐游戏", "视觉小说", "Galgame", "美少女游戏", "乙女游戏",
    "模拟经营", "模拟", "策略", "动作", "射击", "格斗游戏", "平台跳跃",
    "银河恶魔城", "建造", "同人游戏", "文字冒险", "生存游戏",
    # 风格 / 审美（跨类型可用的浏览面）
    "和风", "国风", "治愈系",
}

GENRE_KEYWORDS = (
    "治愈", "日常", "热血", "悬疑", "科幻", "恋爱", "奇幻", "搞笑",
    "音乐", "后宫", "百合", "机战", "运动", "偶像", "校园", "青春",
    "致郁", "猎奇", "扭曲", "异世界", "魔法", "冒险", "战斗", "推理",
    "恐怖", "犯罪", "历史", "战争", "职场", "美食", "末日", "丧尸",
    "转生", "复仇", "武侠", "神话", "吸血鬼", "妖怪",
)

# 口味词展开（推荐匹配）的关键词表：题材判定口径 GENRE_KEYWORDS 之上补「竞技」，
# 让「体育竞技」这类自然语言避雷词能落到站内「运动 / 竞技」标签上。
# 只服务推荐匹配层，不回流 is_genre_tag——成分测试（画像）的判定口径不动。
RECOMMEND_GENRE_KEYWORDS = GENRE_KEYWORDS + ("竞技",)

# 题材同义词表：键是展示用规范名（须在 GENRE_TAGS 内），值是常见别名。
# 归一后写库，标签云与筛选不再因写法不同而分裂。
GENRE_ALIASES = {
    "科幻": {"SF", "ＳＦ", "sci-fi", "SciFi", "科学幻想", "科幻小说"},
    "治愈": {"癒し", "愈し", "療癒", "治愈系"},
    "日常": {"日常系", "Slice of Life", "slice of life"},
    "搞笑": {"喜劇", "喜剧", "コメディ", "逗比", "搞笑番"},
    "恋爱": {"恋愛", "純愛", "爱情", "ロマンス"},
    "悬疑": {"懸疑", "mystery", "Mystery", "悬疑剧"},
    "推理": {"ミステリー", "侦破", "侦探"},
    "奇幻": {"ファンタジー", "幻想", "Fantasia"},
    "百合": {"GL", "ガールズラブ", "Girls Love"},
    "耽美": {"BL", "ボーイズラブ", "Boys Love"},
    "机战": {"ロボット", "机器人动画", "超级机器人", "真实系"},
    "偶像": {"アイドル", "idol"},
    "音乐": {"音楽", "乐队番"},
    "运动": {"スポーツ", "体育"},
    "校园": {"学園", "学园", "学校"},
    "冒险": {"冒険", "アドベンチャー", "探险"},
    "战斗": {"バトル", "戦闘", "战斗番"},
    "魔法少女": {"魔女っ子", "马猴烧酒"},
    "特摄": {"特撮", "超级英雄", "超级战队"},
    "恐怖": {"ホラー", "灵异"},
    "历史": {"時代劇", "时代剧", "史剧"},
    "战争": {"戦争", "军事"},
    "美食": {"料理", "グルメ", "美食番"},
    "职场": {"会社", "OL", "工作", "社畜"},
    "末日": {"末世", "后启示录", "废土", "Apocalypse", "末日求生"},
    "丧尸": {"僵尸", "ゾンビ", "zombie", "Zombie"},
    "转生": {"転生", "异世界转生", "異世界転生", "reincarnation"},
    "复仇": {"復讐", "报复", "复仇剧"},
    "武侠": {"武俠", "wuxia", "江湖"},
    "智斗": {"心理战", "智鬥", "头脑战", "智斗番"},
    "神怪": {"伝奇", "怪谈", "神魔", "灵怪"},
    "群像": {"群像劇", "群像剧"},
    "单元剧": {"短編集", "单元喜剧"},
    "热血": {"熱血", "燃系", "少年漫"},
    "萌": {"萌え", "萌豚"},
    "傲娇": {"ツンデレ"},
    "后宫": {"ハーレム", "后宫番"},
    "青梅竹马": {"幼馴染", "青梅竹馬"},
    "同居": {"同棲", "同居人"},
    "三角关系": {"三角関係", "三角恋"},
    "吸血鬼": {"ヴァンパイア", "vampire", "Vampire"},
    "龙": {"ドラゴン", "dragon", "Dragon", "龍"},
    "克苏鲁": {"克蘇魯", "Cthulhu", "cthulhu"},
    "蒸汽朋克": {"スチームパンク", "steampunk", "Steampunk"},
    "赛博朋克": {"サイバーパンク", "cyberpunk", "Cyberpunk", "赛博"},
    "太空": {"宇宙", "space", "Space", "星际"},
    "竞技": {"競技", "esports", "电竞", "比赛"},
    "生存": {"サバイバル", "survival", "Survival"},
    "海盗": {"海賊", "pirate", "Pirate"},
    "西部": {"西部劇", "western", "Western"},
    "三国": {"三国志", "三国演义"},
    "剧情": {"ドラマ", "剧情向", "剧情片"},
    "猎奇": {"猟奇", "奇谈"},
    "致郁": {"鬱", "压抑", "致郁系"},
    "黑深残": {"黑暗", "暗黑"},
    "恋爱喜剧": {"恋愛コメディ", "恋爱搞笑", "ラブコメ"},
    "异世界": {"異世界", "异世界幻想"},
    "Galgame": {"galgame", "美少女游戏", "GALGAME"},
    "视觉小说": {"ビジュアルノベル", "文字冒险游戏"},
    "Roguelike": {"roguelike", "肉鸽", "肉鸽like"},
}

# 制作公司别名（Bangumi 标签里常见写法）；键是展示用规范名。
STUDIO_ALIASES = {
    "京都动画": {"京都动画", "京阿尼", "Kyoto Animation", "京都アニメーション"},
    "MAPPA": {"MAPPA", "mappa"},
    "ufotable": {"ufotable", "飞碟社", "Ufotable"},
    "A-1 Pictures": {"A-1 Pictures", "A1 Pictures", "A-1Pictures"},
    "J.C.STAFF": {"J.C.STAFF", "JCSTAFF", "J.C.Staff"},
    "动画工房": {"动画工房", "動画工房", "Doga Kobo"},
    "Production I.G": {"Production.I.G", "Production I.G", "ProductionIG"},
    "BONES": {"BONES", "骨头社", "Bones"},
    "SUNRISE": {"SUNRISE", "日升", "Sunrise", "sunrise"},
    "P.A.WORKS": {"P.A.WORKS", "PAWORKS", "P.A.Works"},
    "SHAFT": {"SHAFT", "SHAFT", "Shaft"},
    "TRIGGER": {"TRIGGER", "Trigger", "扳机社"},
    "MADHOUSE": {"MADHOUSE", "Madhouse", "疯房子"},
    "WIT STUDIO": {"WIT STUDIO", "WIT Studio", "Wit Studio", "霸权社"},
    "CloverWorks": {"CloverWorks", "Clover Works"},
    "GAINAX": {"GAINAX", "Gainax", "宅社"},
    "Khara": {"Khara", "khara"},
    "吉卜力工作室": {"吉卜力", "Studio Ghibli", "Ghibli", "GHIBLI"},
    "东映动画": {"东映动画", "Toei Animation", "东映"},
    "Studio Pierrot": {"Studio Pierrot", "小丑社", "Pierrot"},
    "SILVER LINK.": {"SILVER LINK.", "Silver Link", "银链"},
    "8bit": {"8bit", "8-Bit", "Eight Bit"},
    "Brain's Base": {"Brain's Base", "Brains Base"},
    "OLM": {"OLM", "OLM TEAM", "オー・エル・エム"},
    "Telecom Animation Film": {"Telecom Animation Film", "Telecom"},
    "TMS Entertainment": {"TMS Entertainment", "TMS"},
    "龙之子制作": {"龙之子制作", "タツノコプロ", "龙之子"},
    "Science SARU": {"Science SARU", "科学猴子", "Science SARU"},
    "LIDENFILMS": {"LIDENFILMS", "Lidenfilms", "ライデンフィルム"},
    "讲谈社": {"讲谈社", "講談社", "Kodansha"},
    "集英社": {"集英社", "Shueisha"},
    "小学馆": {"小学馆", "小學館", "Shogakukan"},
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
    "动画",
    "動画",
)

# 明确属于噪音的标签：媒介、载体、平台、地区、季度与元标签。
NOISE_TAGS = {
    # 放送形态 / 媒介
    "tv", "剧场版", "映画", "ova", "oad", "web", "动画", "アニメ", "动画电影",
    "TV动画", "电视动画", "网络动画", "WEB动画", "国产动画", "日本动画",
    "原创动画", "短篇动画", "短篇", "长篇", "泡面番", "短片", "番剧",
    "季番", "半年番", "年番", "mv", "cm", "pv", "动态漫画", "广播剧", "朗读剧",
    # 原作载体 / 企划形态
    "原创", "漫画原作", "小说原作", "游戏原作", "多媒体企划",
    "漫画改", "漫画改编", "小说改", "游戏改", "轻改", "漫改", "动画化",
    # 载体（本站四类型之外的「这是什么」）
    "漫画", "小说", "游戏", "动漫", "二次元", "ACG", "ACGN", "新番",
    # 画质 / 版本（不是题材）
    "BD", "蓝光", "高清", "4K", "重制", "修复", "生肉", "熟肉", "译制",
    # 地区 / 国别
    "国产", "国漫", "日漫", "美漫", "欧美", "日系", "日本", "中国", "美国",
    "韩国", "英国", "法国", "港台", "香港", "台湾", "大陆", "华语",
    # 平台（本站没有平台维度；RPG/ACT 等类型词是题材，见 GENRE_TAGS）
    "PC", "PS", "PS2", "PS3", "PS4", "PS5", "PSP", "PSV", "PSVita", "Switch",
    "NS", "NintendoSwitch", "Xbox", "XboxOne", "XboxSeries", "手机", "安卓",
    "Android", "iOS", "iPhone", "iPad", "网页", "页游", "H5", "街机", "VR",
    "Steam", "Epic", "主机", "掌机", "模拟器",
    # 元标签（主观推荐语，不是题材）
    "神作", "经典", "童年", "回忆", "入坑", "补番", "佳作", "冷门", "神番",
    "好看", "安利", "收藏", "待看", "已看",
}

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_SUMMARY_YEAR_RE = re.compile(r"(19|20)\d{2}(?=\s*年)")
# 季度标签（2011年10月 / 2021年1月）：Bangumi 常见写法，发行年由 date 承载。
_SEASON_TAG_RE = re.compile(r"^(19|20)\d{2}年")


def _norm_key(name: str) -> str:
    """归一查找键：NFKC（全角→半角、半角ｶﾀｶﾅ→片假名）+ casefold。

    让 `ＳＦ`、`sf`、`Sf` 都能命中同一个规范名；日文假名不受 casefold 影响。
    """
    return unicodedata.normalize("NFKC", (name or "").strip()).casefold()


# 规范名查找表：规范名自身 + 全部别名，键为 _norm_key。
_GENRE_LOOKUP: dict[str, str] = {}
for _genre in GENRE_TAGS:
    _GENRE_LOOKUP[_norm_key(_genre)] = _genre
for _canonical, _aliases in GENRE_ALIASES.items():
    _GENRE_LOOKUP[_norm_key(_canonical)] = _canonical
    for _alias in _aliases:
        _GENRE_LOOKUP[_norm_key(_alias)] = _canonical

_NOISE_LOOKUP = {_norm_key(name) for name in NOISE_TAGS}

_STUDIO_LOOKUP = {
    _norm_key(alias): canonical
    for canonical, aliases in STUDIO_ALIASES.items()
    for alias in aliases
}


def canonical_genre(name: str) -> str | None:
    """题材规范名：命中同义词表返回规范名（SF -> 科幻），否则 None。"""
    key = _norm_key(name)
    if not key:
        return None
    return _GENRE_LOOKUP.get(key)


def is_genre_tag(tag: str) -> bool:
    """是否为题材类标签（画像侧口径）。"""
    if not tag:
        return False
    if _YEAR_RE.match(tag):
        return False
    if canonical_genre(tag):
        return True
    # 关键词子串兜底：源站长尾写法（「异世界转生」「治愈系日常」）仍算题材，
    # 但展示时保留原名，不做强行改写（避免把「不日常」改错）。
    haystack = _norm_key(tag)
    return any(keyword in haystack for keyword in GENRE_KEYWORDS)


_GENRE_ALIAS_PAIRS: list[tuple[str, str]] | None = None


def _genre_alias_pairs() -> list[tuple[str, str]]:
    """(归一别名, 规范名) 列表：子串匹配要「名字含别名」，GENRE_ALIASES 得反着查。"""
    global _GENRE_ALIAS_PAIRS
    if _GENRE_ALIAS_PAIRS is None:
        _GENRE_ALIAS_PAIRS = [
            (_norm_key(alias), canonical)
            for canonical, aliases in GENRE_ALIASES.items()
            for alias in aliases
            if alias
        ]
    return _GENRE_ALIAS_PAIRS


def expand_genre_names(names: Iterable[str], site_tags: Iterable[str]) -> list[str]:
    """把口味词展开成站内真实标签名（按口味推荐的匹配层，2026-10-19）。

    口味词的写法天生比站内规范标签野，精确对撞会大量落空：
    - 画像 ``top_genres`` 保留 Bangumi 原始写法（``is_genre_tag`` 只判定不改写），
      「日常系」「搞笑番」原样进画像；
    - 用户自然语言更随意：「致郁系」「体育竞技」「轻喜剧」「硬核科幻」。
    避雷词一旦落空，一票否决会静默失效——推荐「没生效」的根因之一。

    展开规则（按序全部合并，不是短路；只保留站内真实存在的标签名）：
    1. 原名就在站内标签里 → 直接用；
    2. 同义词规范名（致郁系→致郁、日常系→日常）在站内 → 加入；
    3. 名字含某条别名（「轻喜剧」含「喜剧」→搞笑、「体育竞技」含「体育」→运动）→ 加入；
    4. 名字含 ``RECOMMEND_GENRE_KEYWORDS`` 里的题材关键词
       （「硬核科幻」含科幻、「扭曲情感」含扭曲、「体育竞技」含竞技）→ 加入该关键词。
    返回按首次出现顺序去重的列表，保证命中展示顺序稳定。
    """
    site = {str(tag) for tag in site_tags}
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        candidates = [name]
        canonical = canonical_genre(name)
        if canonical and canonical != name:
            candidates.append(canonical)
        norm = _norm_key(name)
        for alias, target in _genre_alias_pairs():
            if alias in norm:
                candidates.append(target)
        for keyword in RECOMMEND_GENRE_KEYWORDS:
            if keyword in norm:
                candidates.append(keyword)
        for cand in candidates:
            if cand in site and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def studio_name(tag: str) -> str | None:
    """标签若是制作公司，返回展示用规范名；否则 None。"""
    name = (tag or "").strip()
    if not name:
        return None
    canonical = _STUDIO_LOOKUP.get(_norm_key(name))
    if canonical:
        return canonical
    lowered = _norm_key(name)
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
    ``year`` 的值是四位数字符串，``studio`` 是规范名，genre 命中同义词表时
    返回规范名（否则原样），其余原样（已 strip）。

    顺序即优先级：年份 -> 明确噪音（平台/地区/季度/元标签，精确命中）-> 公司
    -> 题材 -> 噪音。噪音精确匹配放在公司之前：否则「国产动画」这类词会被
    公司后缀规则误收成制作方。
    """
    name = (tag or "").strip()
    if not name:
        return TAG_NOISE, ""
    year = year_of(name)
    if year is not None:
        return TAG_YEAR, str(year)
    key = _norm_key(name)
    if key in _NOISE_LOOKUP or _SEASON_TAG_RE.match(name):
        return TAG_NOISE, name
    studio = studio_name(name)
    if studio:
        return TAG_STUDIO, studio
    canonical = canonical_genre(name)
    if canonical:
        return TAG_GENRE, canonical
    if is_genre_tag(name):
        return TAG_GENRE, name
    return TAG_NOISE, name


def split_tags(tags) -> tuple[list[str], list[str], int | None]:
    """把标签列表拆成 (题材, 公司, 年份)。

    - 题材与公司保序去重（题材命中同义词表时写规范名）；
    - 年份取先出现的那个；
    - 噪音（声优 / 载体 / 平台 / 季度）直接丢弃；
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
