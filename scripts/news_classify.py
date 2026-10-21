"""资讯分类与优先级（纯函数，无第三方依赖、不触网、不依赖其它脚本）。

设计原则（与产品定稿一致）：分类 / 事件 / 优先级 三者严格分离——
- **主体类型（subject）**：这条资讯属于哪个 ACGN 主体？动画 / 漫画 / 轻小说 / 游戏 /
  综合动态。是面向用户的分类（"做面向用户的分类"），音乐 / 创作者 / 业界不另立主体，
  而是按其关联作品归入四类之一，关联不上才落 综合动态。
- **事件类型（event_type）**：发生了什么？新作发布 / 发售 / 歌曲发布 / 创作者动态 /
  健康状况 / 制作消息 / 行业事件 / 其他。辅助分类，不替代主体类型。
- **地区（region）**：日本 / 国产 / 欧美 / 其他。仅作内部权重参考，不成为面向用户的分类。
- **优先级（priority_score）**：应该排多前？由 主体 + 游戏子类 + 地区 + 事件重要性 +
  来源类型 叠加计算，落库后供首页 / 资讯频道排序。

游戏子类（仅游戏有意义）：Galgame / 二游 / 其他游戏。galge 是游戏内最高优先级，
日本二游与国产二游同权（子类不受地区惩罚），日本其它游戏次之，国产非二游适当降权，
欧美普通内容低优先级、欧美重大事件可破格提升（但不凭标题关键词随意判定）。

所有检测都是基于标题 / 摘要的中日英关键词启发式，简单、可解释、易调整；命中不到就回落默认值。
"""

from __future__ import annotations

# ---------- 取值常量（面向用户的主体类型 + 内部维度） ----------

# 主体类型（用户可见分类）：四类 ACGN 主体 + 综合动态兜底。
SUBJECT_ANIME = "动画"
SUBJECT_MANGA = "漫画"
SUBJECT_LN = "轻小说"
SUBJECT_GAME = "游戏"
SUBJECT_GENERAL = "综合动态"  # "综合资讯" 的历史别名统一归到这里
SUBJECT_TYPES = (SUBJECT_ANIME, SUBJECT_MANGA, SUBJECT_LN, SUBJECT_GAME, SUBJECT_GENERAL)
_FALLBACK_SUBJECT = SUBJECT_GENERAL

# 游戏子类（仅 category == 游戏 时有意义）。
GAME_SUB_GALGE = "Galgame"
GAME_SUB_GACHA = "二游"
GAME_SUB_OTHER = "其他游戏"
GAME_SUBCATS = (GAME_SUB_GALGE, GAME_SUB_GACHA, GAME_SUB_OTHER)

# 事件类型（辅助分类）。
EVENT_NEW = "新作发布"
EVENT_RELEASE = "发售"
EVENT_SONG = "歌曲发布"
EVENT_CREATOR = "创作者动态"
EVENT_HEALTH = "健康状况"
EVENT_PRODUCTION = "制作消息"
EVENT_INDUSTRY = "行业事件"
EVENT_OTHER = "其他"
EVENT_TYPES = (
    EVENT_HEALTH, EVENT_INDUSTRY, EVENT_NEW, EVENT_SONG, EVENT_RELEASE,
    EVENT_PRODUCTION, EVENT_CREATOR, EVENT_OTHER,
)

# 地区（内部权重，不面向用户）。
REGION_JP = "日本"
REGION_CN = "国产"
REGION_WEST = "欧美"
REGION_OTHER = "其他"
REGIONS = (REGION_JP, REGION_CN, REGION_WEST, REGION_OTHER)

# 来源类型（用于排序与折叠，不另立主体分类）。
SOURCE_TYPE_MEDIA = "媒体编译"   # 机核 / 游民 等中文编译源
SOURCE_TYPE_ORIGINAL = "原文"     # 日文原文（配翻译后）
SOURCE_TYPE_PR = "官方PR"        # Aniplex 等官方宣发
SOURCE_TYPE_USER = "用户投稿"
SOURCE_TYPES = (SOURCE_TYPE_MEDIA, SOURCE_TYPE_ORIGINAL, SOURCE_TYPE_PR, SOURCE_TYPE_USER)

# 综合资讯 的历史别名 → 综合动态。
_SUBJECT_ALIASES = {
    "综合资讯": SUBJECT_GENERAL,
    "动漫资讯": SUBJECT_ANIME,   # Aniplex 的栏目名，按其性质归动画
    "榜单资讯": SUBJECT_GENERAL,
    "活动资讯": SUBJECT_GENERAL,
}


def normalize_subject(category: str) -> str:
    """把任意栏目名归一化到受控主体类型；命中不到回落 综合动态。"""
    text = (category or "").strip()
    if text in SUBJECT_TYPES:
        return text
    return _SUBJECT_ALIASES.get(text, _FALLBACK_SUBJECT)


# ---------- 主体分类（按内容判定，不再只认源栏目） ----------
# 2026-10-21 定稿：分类必须依据资讯实际主题，不能只取 SourceSpec.category
# （否则 animeanime 的全是动画、机核的全是综合动态，漫画 / 轻小说永远空转）。
# 加权计分：强信号（只有该主体会用的词）2 分，弱信号（通称）1 分；
# 分数最高者胜，平分看源栏目，再平分回落综合动态。
# **不依赖作品库关联**：关联不到作品不是分类失败的理由。
_SUBJECT_STRONG_SIGNALS: dict[str, tuple[str, ...]] = {
    # 「単行本 / 巻」漫画和轻小说都有，不算区分信号，不放进来
    SUBJECT_MANGA: (
        "連載", "週刊", "ジャンプ", "マガジン", "サンデー", "チャンピオン",
        "漫画家", "作画", "コミカライズ", "コミックス", "連載再開", "休載",
    ),
    SUBJECT_LN: (
        "ライトノベル", "ラノベ", "文庫", "ISBN", "ノベルス", "小説家",
        "軽小説", "轻小说", "書籍化",
    ),
    SUBJECT_GAME: (
        "ガチャ", "シナリオ", "実装", "アップデート", "バージョン", "ver.", "dlc",
        "プラットフォーム", "steam", "プレイステーション", "nintendo", "switch",
        "サービス終了", "停服", "事前登録", "リリース決定", "マップ",
        "プレイアブル", "コラボイベント", "★", "買い切り",
    ),
    # 动画强信号只收**动画专属**词。「アニメ / 映画 / 配信開始 / PV」动画和游戏都在用
    # （动画 IP 改编游戏的公告里全是这些），收进来会把游戏稿抢成动画，所以降为弱信号。
    SUBJECT_ANIME: (
        "放送", "劇場", "主題歌", "オープニング", "エンディング",
        "声優", "キャスト", "スタッフ", "監督", "制作", "作画",
        "オーディション", "キャラクターボイス", "連続放送",
    ),
}

_SUBJECT_WEAK_SIGNALS: dict[str, tuple[str, ...]] = {
    SUBJECT_ANIME: ("アニメ", "动画", "动漫", "番组", "番組", "新番", "映画", "配信開始", "pv"),
    SUBJECT_MANGA: ("漫画", "コミック"),
    SUBJECT_LN: ("小说", "小説"),
    SUBJECT_GAME: ("游戏", "手游", "手机游戏", "ゲーム"),
}

# 音乐 / 文化 / 行业动态：不另立主体，落到「综合」。
# 音乐人（Ado / ヨルシカ 等）即便关联不到具体作品，也按其文化属性归综合，不丢。
_MUSIC_CULTURE_SIGNALS: tuple[str, ...] = (
    "新曲", "アルバム", "ライブ", "ツアー", "音楽", "音乐", "歌手", "バンド", "ボカロ",
    "作曲家", "乐曲", "演唱", "コンサート", "フェス", "主題歌", "コラボソング",
)


def classify_subject_from_text(
    title: str, summary: str, fallback: str = _FALLBACK_SUBJECT
) -> str:
    """按标题 / 摘要的实际内容判主体（动画 / 漫画 / 轻小说 / 游戏 / 综合动态）。

    - 强信号 2 分、弱信号 1 分，最高分者胜；平分用 fallback（一般是源栏目）；
    - 完全没有四类信号时回落 fallback；fallback 也不可用时回落综合动态；
    - 音乐 / 文化 / 行业信号只在四类都没命中时把人送进「综合」（不是兜底垃圾桶，
      进综合的前提是已通过收录判定，见 scripts/news_filters）。
    """
    text = f"{title}\n{summary}".lower()
    scores: dict[str, int] = {}
    for subject, words in _SUBJECT_STRONG_SIGNALS.items():
        score = sum(2 for word in words if word.lower() in text)
        if score:
            scores[subject] = score
    for subject, words in _SUBJECT_WEAK_SIGNALS.items():
        score = sum(1 for word in words if word.lower() in text)
        if score:
            scores[subject] = scores.get(subject, 0) + score

    if scores:
        best = max(scores.values())
        winners = [subject for subject, score in scores.items() if score == best]
        if len(winners) == 1:
            return winners[0]
        normalized = normalize_subject(fallback)
        if normalized in winners:
            return normalized
        # 平分且源栏目不在其中：动画 / 游戏是站内主力，优先给这两个
        for preferred in (SUBJECT_ANIME, SUBJECT_GAME):
            if preferred in winners:
                return preferred
        return winners[0]

    if any(word.lower() in text for word in _MUSIC_CULTURE_SIGNALS):
        return SUBJECT_GENERAL
    return normalize_subject(fallback)


# ---------- 游戏子类检测（仅 category == 游戏 时调用） ----------

_GALGE_KEYWORDS = (
    "ギャルゲー", "ガルゲー", "美少女ゲーム", "ビジュアルノベル", "ノベルゲー",
    "恋愛ゲーム", "美少女ゲーム", "BLゲーム", "乙女ゲーム", " galge", "galgame",
    "galge", "galgame", "视觉小说", "美少女游戏", "恋爱游戏", "乙女游戏", "bl游戏",
)

# 真正的二次元游戏（二游）：含抽卡 / 手游 / 社交游戏语境；不包含所有国产手游。
_GACHA_KEYWORDS = (
    "ガチャ", "ソシャゲ", "ソーシャルゲーム", "スマホゲー", "スマートフォンゲーム",
    "モバイルゲーム", "ブラウザゲーム", "二次元ゲーム", "二游", "手游", "手机游戏",
    "二次元手游", "抽卡", "卡牌手游",
)


def classify_subcategory(title: str, summary: str, category: str) -> str:
    """游戏的子类：Galgame / 二游 / 其他游戏；非游戏类返回空串。"""
    if normalize_subject(category) != SUBJECT_GAME:
        return ""
    text = f"{title}\n{summary}".lower()
    if any(k.lower() in text for k in _GALGE_KEYWORDS):
        return GAME_SUB_GALGE
    if any(k.lower() in text for k in _GACHA_KEYWORDS):
        return GAME_SUB_GACHA
    return GAME_SUB_OTHER


# ---------- 地区检测（内部权重，不面向用户） ----------

# 国产信号：明确的中国 / 国産 词，或中国厂商名（二次元手游厂优先）。
_CN_KEYWORDS = (
    "国産", "国产", "中国", "本土", "国服", "国区", "国産",
    "哔哩哔哩", "bilibili", "腾讯", "米哈游", "鹰角", "网易", "叠纸", "莉莉丝",
    "三七互娱", "字节", "抖音", "华为游戏", "完美世界", "散爆", "库洛",
)

# 欧美信号：明确的欧美词，或欧美厂商 / 平台（刻意排除任天堂 / 索尼 / 卡普空等日本厂商）。
# 只用够长的特征串：像 "ea" 这种两字母片段会误伤 idea / team / sea 等普通英文词。
_WEST_KEYWORDS = (
    "欧米", "欧州", "アメリカ", "米国", "西洋", "欧美", "海外", "洋ゲー",
    "disney", "marvel", "dcコミックス", "ubisoft", "blizzard", "暴雪",
    "xbox", "microsoft", "微软", "gta", "rockstar", "bethesda", "steam",
    "netflix", "warner", "sony interactive", "epic games", "activision",
)


def detect_region(default_region: str, title: str, summary: str) -> str:
    """在源的默认地区基础上，用标题 / 摘要里的国産 / 欧美信号细化；命中不到用默认值。"""
    text = f"{title}\n{summary}".lower()
    if any(k.lower() in text for k in _CN_KEYWORDS):
        return REGION_CN
    if any(k.lower() in text for k in _WEST_KEYWORDS):
        return REGION_WEST
    return default_region


# ---------- 事件类型检测（辅助分类） ----------

# 按顺序匹配：靠前的优先级更高，避免被后面的泛词误标。
_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (EVENT_HEALTH, (
        "健康", "病", "逝", "死去", "死亡", "休養", "休养", "引退", "結婚", "结婚",
        "離婚", "离婚", "容体", "闘病", "斗病", "容態", "入院", "退院", "訃報", "讣闻",
        "死去", "急死", "逝去", "health", "passed away", "retire",
    )),
    (EVENT_INDUSTRY, (
        "業界", "倒産", "倒产", "買収", "收购", "合併", "合并", "上場", "上市",
        "提携", "合作", "決算", "业绩", "統合", "整合", "廃止", "终止", "サービス終了",
        "停服", "关服", "終了", "炎上", "诉讼", "訴訟", "判決", "判决", "規制", "监管",
        "ストライキ", "罢工", "重组", "破産", "破产", "裁员", "リストラ", "事件",
        "业界", "关停", "关停",
    )),
    (EVENT_NEW, (
        "新作", "発表", "制作決定", "製作決定", "アニメ化", "新シリーズ", "新プロジェクト",
        "新番", "新动画", "新ゲーム", "新作発表", "新作ゲーム", "企划", "企劃",
        "announce", "公布", "披露", "新作游戏", "新系列", "新项目",
    )),
    (EVENT_SONG, (
        "主題歌", "主題曲", "テーマソング", "オープニング", "エンディング", "挿入歌",
        "op曲", "ed曲", "歌", "リリース", "配信開始", "歌曲", "单曲", "單曲", "专辑",
        "專輯", "主题曲", "主題歌", "mv", "楽曲", "乐曲", "演唱", "sing",
    )),
    (EVENT_RELEASE, (
        "発売", "発売日", "発売決定", "予約開始", "予約", "発売予定", "販売",
        "发售", "开售", "上架", "发售日", "预约", "限定版", "限定盤", "発売記念",
    )),
    (EVENT_PRODUCTION, (
        "スタッフ", "キャスト", "スタジオ", "監督", "脚本", "制作進行", "制作発表",
        "制作公司", "配音", "声優", "声优", "制作", "スタッフ発表", "キャスト発表",
        "制作委員会", "制作委员会", "导演", "编剧", "作画", "动画制作",
    )),
    (EVENT_CREATOR, (
        "作者", "漫画家", "作家", "脚本家", "原作者", "プロデューサー", "プロデューサ",
        "アーティスト", "クリエイター", "歌手", "画师", "画師", "デザイナー", "設計",
        "制作人", "编曲", "作曲家", "漫畫家", "連載", "连载", "執筆", "执笔",
    )),
)


def classify_event_type(title: str, summary: str) -> str:
    """从标题 / 摘要推断事件类型；按顺序匹配，命中不到回落 其他。"""
    text = f"{title}\n{summary}".lower()
    for event_type, keywords in _EVENT_RULES:
        if any(k.lower() in text for k in keywords):
            return event_type
    return EVENT_OTHER


# ---------- 成人内容标记（展示层隐藏，不删除、不过滤） ----------

# 成人向游戏 / R-18 信号：只用来给资讯打 is_adult 标记，由「隐藏成人内容」开关在展示层遮蔽，
# 绝不据此过滤掉资讯（galge 反而要最高优先级）。
_ADULT_KEYWORDS = (
    "18禁", "r18", "エロゲ", "エロゲー", "美少女ゲーム", "アダルトゲーム", "成人游戏",
    "里都合", "成人向け", "年齢制限", "年龄限制", "blゲーム",
)


def flag_adult(title: str, summary: str) -> bool:
    """是否标记为成人向（展示层按全局开关隐藏图片与露骨摘要）。"""
    text = f"{title}\n{summary}".lower()
    return any(k.lower() in text for k in _ADULT_KEYWORDS)


# ---------- 优先级计算（落库，供排序） ----------

# 主体基础分：动画 / 漫画 / 轻小说 顶档；游戏与**综合动态同权**（2026-10-21 用户定稿：
# 综合动态不再是「压低一档的兜底」，抓取配额与游戏同级）。
_SUBJECT_BASE = {
    SUBJECT_ANIME: 100,
    SUBJECT_MANGA: 100,
    SUBJECT_LN: 100,
    SUBJECT_GAME: 80,
    SUBJECT_GENERAL: 80,
}

# 游戏子类加分：galge 最高；二游次之；其它游戏不加分。
# 二游加分刻意低于「动画/漫画/轻小说 vs 游戏」的 20 分主体差：日本动画 / 漫画 / 轻小说
# 与 Galgame 是顶级优先，二游（日本 = 国产同权）紧随其后，日本其它游戏再次之。
_GAME_SUB_BONUS = {
    GAME_SUB_GALGE: 40,   # galge 是游戏内最高优先级
    GAME_SUB_GACHA: 12,   # 二游（日本 = 国产同权，见下方地区处理）
    GAME_SUB_OTHER: 0,
}

# 地区加分：日本最高；国产基础 0；欧美普通 -10（重大可破格）。
_REGION_BONUS = {
    REGION_JP: 12,
    REGION_CN: 0,
    REGION_WEST: -10,
    REGION_OTHER: 0,
}

# 事件重要性加分：健康状况必须不被沉底；行业事件重大才加分。
_EVENT_BONUS = {
    EVENT_HEALTH: 30,
    EVENT_INDUSTRY: 22,
    EVENT_NEW: 12,
    EVENT_PRODUCTION: 8,
    EVENT_CREATOR: 8,
    EVENT_SONG: 5,
    EVENT_RELEASE: 5,
    EVENT_OTHER: 0,
}

# 来源类型加分：媒体编译略高；官方 PR 明显压低（折叠出主 feed）。
_SOURCE_TYPE_BONUS = {
    SOURCE_TYPE_MEDIA: 5,
    SOURCE_TYPE_ORIGINAL: 0,
    SOURCE_TYPE_USER: -5,
    SOURCE_TYPE_PR: -25,
}

# 商品宣传降权（2026-10-21 用户：商品宣传的权重降低）。
# 周边 / 联名 / 抽奖贩售这类稿子信息量低、占版面，但**不该删**（ACGN IP 的周边仍是内容），
# 所以只降优先级：让首页精选把它们排到作品动态之后，列表页仍按时间能看到。
# 判据只看标题 / 摘要（正文里提一句不算）；与 news_filters 的商品词表分开维护，
# 本模块不依赖其它脚本（避免循环导入）。
MERCH_PROMO_WORDS: tuple[str, ...] = (
    "グッズ", "コラボ", "一番くじ", "フィギュア", "ぬいぐるみ", "バッグ", "ポーチ",
    "チャーム", "ネックレス", "ピアス", "指輪", "ブレスレット", "雑貨", "Tシャツ",
    "靴下", "マグカップ", "クッション", "タオル", "ポストカード", "プライズ",
    "食玩", "缶セット", "キーホルダー", "アクセサリー", "受注販売", "先行販売",
    "予約受付", "発売記念", "限定ショップ", "アベイル", "しまむら", "サンキューマート",
)
MERCH_PROMO_PENALTY = -30

# ---------- 重大官宣（每日配额豁免） ----------
# 2026-10-22 用户定稿：动画化 / 制作決定 / 续作決定 这类官宣是站内最值钱的一类，
# 不能因为当天普通配额被灌满就收不进来（RSS feed 存活期一过就永久拿不回来），
# 所以它们走**独立豁免额度**入库，不占 news_daily_quota（见 scripts/news_daily）。
#
# **判定必须是短语级，不能是「裸形态词 × 决定词」**：2026-10-22 用线上 286 条真实标题
# 回归，词级组合命中 27 条、其中 17 条是误判——Aniplex 官方 PR 的标题几乎都带
# 「TVアニメ」「劇場アニメ」前缀，于是「Blu-ray全4巻が発売決定」「TVer配信決定」
# 「再放送決定」「15周年企画始動」「ステージ参加決定」「POP UP STORE開催決定」
# 全被判成官宣。改成：只认「化」类固定搭配 + 「续作/剧场版/重制 + 决定词」+ 中文「官宣」，
# 并且先过一遍商品 / 活动 / 放映 / 报道体裁的排除词。
MAJOR_STRONG = 3   # 「化」类：形态转换自带决定含义（アニメ化 / 実写化 / コミカライズ…）
MAJOR_SEQUEL = 2   # 续作 / 剧场版 / 重制：需标题里另有决定词（続編 + 製作決定）
MAJOR_OFFICIAL = 1  # 中文「官宣 / 宣布制作 / 立项」：中文源的最强信号词
MAJOR_NONE = 0

# 排除词：命中即不算官宣（不管形态词多硬）。这里收的是**商品 / 活动 / 放映 / 报道体裁**，
# 刻意不收「放送 / 配信」以外会误伤真官宣的词——例如「第3期の制作が決定、2027年放送」
# 含「放送」但是正经续作官宣，所以只排「再放送」。
_MAJOR_EXCLUDE_WORDS: tuple[str, ...] = (
    # 商品 / 圆盘 / 票券（"发售决定"是卖货，不是制作决定）
    "発売", "販売", "予約", "受注", "グッズ", "blu-ray", "ブルーレイ", "dvd", "円盤",
    "チケット", "セール", "ストア", "pop up", "ポップアップ", "一番くじ", "フィギュア",
    "发售", "开售", "周边", "预售",
    # 放映 / 配信 / 上映（"配信决定""再放送决定"是上线安排，不是新作官宣）
    "配信", "tver", "abema", "見放題", "再放送", "一挙上映", "上映イベント",
    # 活动 / 联名 / 纪念（"開催决定""コラボ决定"是活动，不是作品决定）
    "イベント", "開催", "ステージ", "コラボ", "キャンペーン", "記念", "周年",
    "联动", "纪念", "活动",
    # 宣传物 / 报道体裁（"最終予告公開""受賞結果が発表""リメイクの定義"是稿子，不是官宣）
    "予告", "cm", "受賞", "インタビュー", "レビュー", "レポート", "コラム", "対談",
    "定義", "声明", "争议", "招聘", "解説", "まとめ",
)

# 「化」类：单信号命中即官宣。这些词本身含"形态发生改变"的语义，不需要再配决定词。
_MAJOR_FORM_WORDS: tuple[str, ...] = (
    "アニメ化", "アニメーション化", "実写化", "実写ドラマ化", "ドラマ化", "映画化",
    "映像化", "コミカライズ", "漫画化", "ノベライズ", "小説化", "ゲーム化", "舞台化",
    "动画化", "动漫化", "真人化", "小说化", "电影化",
)

# 需配决定词的形态：续作 / 剧场版 / 重制。它们本身只是名词，必须搭配"决定/制作/公开"。
_MAJOR_NEEDS_DECISION: tuple[str, ...] = (
    "続編", "続篇", "第2期", "第3期", "第4期", "第5期", "二期", "三期", "四期",
    "新シリーズ", "新シーズン", "新系列", "second season", "season 2", "season 3",
    "劇場版", "劇場アニメ", "リメイク", "リブート", "重置版", "重制",
    "续作", "第二季", "第三季", "重制版", "剧场版",
)

_MAJOR_DECISION_WORDS: tuple[str, ...] = (
    "決定", "决定", "発表", "发表", "制作", "製作", "始動", "公開", "公开",
    "決定へ", "グリーンライト",
)

# 中文「官宣」类：**单用不算**——「《博人传》官宣火影全新劲敌」也是"官宣"，但那是剧情 /
# 设定情报，不是新作 / 制作 / 定档官宣（2026-10-22 用户明确剔除这类）。
# 所以要么配一个"作品形态结果词"（官宣定档 / 官宣改编 / 官宣剧场版），
# 要么本身就是结果短语（宣布制作 / 立项）。
_MAJOR_OFFICIAL_VERBS: tuple[str, ...] = ("官宣", "宣布")
_MAJOR_OFFICIAL_TARGETS: tuple[str, ...] = (
    "定档", "改编", "动画化", "制作", "续作", "续篇", "剧场版", "电影化", "真人化",
    "第二季", "第三季", "第四季", "新作", "新动画", "新企划", "新番",
)
_MAJOR_OFFICIAL_ALONE: tuple[str, ...] = (
    "立项", "宣布制作", "宣布动画化", "宣布改编", "确认制作", "制作确定",
)


def detect_major_announcement(title: str, summary: str = "") -> tuple[int, str]:
    """重大官宣判定，返回 ``(强度, 命中的词)``；非官宣返回 ``(MAJOR_NONE, "")``。

    只看**标题**：RSS 摘要里顺带提一句「アニメ化」不该让整条稿子豁免。
    命中词回传给调用方写运行明细，便于事后校验词表误伤。

    判定顺序：排除词 → 「化」类 → 续作/剧场版/重制 + 决定词 → 中文官宣（动词 + 形态结果词）。
    """
    headline = (title or "").lower()
    if not headline:
        return (MAJOR_NONE, "")
    for word in _MAJOR_EXCLUDE_WORDS:
        if word.lower() in headline:
            return (MAJOR_NONE, "")  # 商品 / 活动 / 放映 / 报道稿：一律不算官宣
    for word in _MAJOR_FORM_WORDS:
        if word.lower() in headline:
            return (MAJOR_STRONG, word)
    forms = [word for word in _MAJOR_NEEDS_DECISION if word.lower() in headline]
    if forms:
        for word in _MAJOR_DECISION_WORDS:
            if word.lower() in headline:
                return (MAJOR_SEQUEL, f"{forms[0]}+{word}")
    for word in _MAJOR_OFFICIAL_ALONE:
        if word.lower() in headline:
            return (MAJOR_OFFICIAL, word)
    verbs = [word for word in _MAJOR_OFFICIAL_VERBS if word in headline]
    if verbs:
        for target in _MAJOR_OFFICIAL_TARGETS:
            if target in headline:
                return (MAJOR_OFFICIAL, f"{verbs[0]}+{target}")
    del summary  # 只认标题（摘要不参与判定），显式丢弃避免误以为漏判
    return (MAJOR_NONE, "")


def is_major_announcement(title: str, summary: str = "") -> bool:
    """是否重大官宣（detect_major_announcement 的布尔包装，便于测试与调用）。"""
    return detect_major_announcement(title, summary)[0] != MAJOR_NONE


# 官宣优先级加成：让豁免条目在首页 / 资讯列表排到普通稿之前
# （连官方 PR 的 -25 与商品宣传的 -30 都能盖过去）。
MAJOR_ANNOUNCEMENT_BONUS = 60

# 日本厂商 / 平台信号：欧美源里出现任天堂等日本厂商时，实际是日本内容，不该被算作欧美。
_JP_VENDOR_SIGNALS = (
    "任天堂", "nintendo", "switch", "playstation", "sega", "capcom", "カプコン",
    "スクウェア", "square enix", "bandai", "バンダイ", "namco", "バンナム",
    "konami", "コナミ", "koei", "コーエー", "atlus", "アトラス", "sony music",
)

# 欧美重大破格信号：命中「公认重大」的作品 / 行业事件才补分。
# 刻意只收窄集合：普通「新作发布」不算重大，否则会把所有欧美资讯抬到日本之上。
_WEST_MAJOR_SIGNALS = (
    "tga", "goty", "the game awards", "年度游戏", "gdc", "e3", "gamescom",
    "现象级", "破圈", "歴代最高", "歴代1位", "破纪录", "記録的", "大ヒット",
    "全世界", "blockbuster",
)


def compute_priority_score(
    *,
    category: str,
    subcategory: str = "",
    region: str = REGION_JP,
    event_type: str = EVENT_OTHER,
    source_type: str = SOURCE_TYPE_MEDIA,
    text: str = "",
) -> int:
    """叠加计算优先级总分。

    - 主体是基础分；
    - 游戏子类只作用于游戏（galge 最高，二游次之）；
    - 地区加分：二游强制享受日本地区加分（日本二游 = 国产二游 同权），
      不受来源实际地区拖累；
    - 事件重要性加分（健康状况必不沉底）；
    - 欧美普通内容被地区扣分；只有命中「公认重大」信号（重大行业事件，或标题 /
      摘要里有公认重大标志）才破格补回；
    - 来源类型：官方 PR 明显压低；
    - 商品宣传（周边 / 联名 / 抽奖贩售）再压一档，让作品动态排在它前面。
    """
    subject = normalize_subject(category)
    score = _SUBJECT_BASE.get(subject, _SUBJECT_BASE[_FALLBACK_SUBJECT])

    if subject == SUBJECT_GAME:
        score += _GAME_SUB_BONUS.get(subcategory, 0)
        # 二游同权：无论实际地区，都按日本地区加分（国产二游不掉队）。
        if subcategory == GAME_SUB_GACHA:
            score += _REGION_BONUS[REGION_JP]
        else:
            score += _REGION_BONUS.get(region, 0)
    else:
        score += _REGION_BONUS.get(region, 0)

    score += _EVENT_BONUS.get(event_type, 0)

    # 欧美重大破格：地区为欧美时，只有「重大行业事件」或标题 / 摘要命中公认重大信号才补分。
    # 普通新作发布 / 发售不算重大——否则欧美内容会被整体抬到日本内容之上。
    if region == REGION_WEST:
        haystack = (text or "").lower()
        jp_vendor = any(s.lower() in haystack for s in _JP_VENDOR_SIGNALS)
        if not jp_vendor:
            if event_type == EVENT_INDUSTRY or any(
                s.lower() in haystack for s in _WEST_MAJOR_SIGNALS
            ):
                score += 30  # 把被地区扣掉的 -10 补回并多给一档

    score += _SOURCE_TYPE_BONUS.get(source_type, 0)

    # 商品宣传降权：标题 / 摘要命中周边联名词 → 明显压低，但仍保留入库与列表展示。
    haystack = (text or "").lower()
    if any(word.lower() in haystack for word in MERCH_PROMO_WORDS):
        score += MERCH_PROMO_PENALTY

    # 重大官宣加成（2026-10-22）：动画化 / 制作決定 / 续作決定 是站内最该被看到的一类，
    # 加到能盖过官方 PR(-25) 与商品宣传(-30)。这里与配额豁免用同一个判定函数，
    # 保证「豁免进来的」和「排在最前的」永远是同一批稿子。
    headline, _, rest = (text or "").partition("\n")
    if detect_major_announcement(headline, rest)[0] != MAJOR_NONE:
        score += MAJOR_ANNOUNCEMENT_BONUS
    return score
