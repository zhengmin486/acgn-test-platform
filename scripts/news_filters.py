"""资讯收录判定：三层过滤（确定性规则 → 内容意图判定 → 低置信度转人工）。

定稿口径（2026-10-21 用户）：
- **收录判定与分类判定分离**：本模块只回答「这条资讯该不该进库」；归入动画 / 漫画 /
  轻小说 / 游戏 / 综合 是 news_classify 的事，两件事不互相依赖。
- **不靠名单打地鼠**：不再维护品牌黑名单、商品词表、来源白名单。判定依据是
  「资讯主体是什么 + 用户看完要做什么」，而不是「出现了哪个词」。
- **ACGN 范围**：作品（动画/漫画/游戏/轻小说）、创作者与制作人员、音乐与文化活动、
  行业动态。三丽鸥 / 姆明 / 迪士尼 / 苹果等主体不能凭名字出现就判定相关或无关，
  一律看资讯实际内容。
- **商业导流排除**：以购买 / 预约 / 抽奖 / 领优惠 / 参与商业活动为主要目的的排除，
  即使涉及宝可梦等 ACGN IP 也不例外。判断主要内容和用户行动目的，不是单个关键词。
- **不得仅凭标题未命中事件词、出现新企划形式、关联不到作品库就过滤**（见
  news_classify，作品库关联只用于上下文，不作为有效性前提）。

三层结构：
- 第一层（确定性）：硬广标记、明确非 ACGN 主体（实写 / 科技）、VOD 导流、抽奖。
  条件苛刻，命中即排除。
- 第二层（内容意图）：区分「作品 / 玩法 / 版本 / 创作者 / 行业动态」与
  「商业促销」。看的是**行动目的**：要用户花钱 / 去外部门店 / 预约实物 → 排除；
  游戏内剧情 / 玩法 / 版本 / 周年内容 → 保留。
- 第三层（低置信度）：规则冲突或信号不足 → **不丢弃**，交给 news_daily 标为待复核
  （照常入库 + 降权 + 记入运行明细），人工校准结果再回流成规则。

本模块不依赖其它脚本（避免 news_sources ↔ import_articles 循环导入），两条管线都从
这里 import ``should_skip_article`` / ``review_reason``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# ---------- 第一层：确定性排除 ----------

# 站方自有节目 / 编辑部内容（2026-10-24 用户定稿）：源的 RSS 里混发的不一定都是文章，
# 还有源站自家的播客 / 电台 / 会员节目。这类**不是资讯**——正文是节目流程、制作署名、
# 编辑部日常（例：机核电台「本期时间轴制作：猪猪米」「内容来自机核办公室」），
# 转过来就是别的站的内部内容，跟 ACGN 动态无关。
#
# 判据两种，命中任一即排除：
# 1. **链接形态**（主）：节目有自己的 URL 路径，与文章天然分开，不随文案变化；
# 2. **内容信号**（兜底）：节目文案里固定出现的署名 / 栏目自称，防 URL 形态改版。
_SOURCE_PROGRAM_URL_MARKERS: tuple[str, ...] = (
    "/radios/",  # 机核 GCORES 电台（核市奇谭 / 录音笔 / GPASS 会员节目等）
)
_SOURCE_PROGRAM_WORDS: tuple[str, ...] = (
    "本期时间轴制作", "内容来自机核办公室", "机核办公室", "机核电台", "gpass",
)

# 固定栏目闲聊（标题以栏目名开头）：编辑随笔，无资讯量。
# 用**标题前缀**匹配而非关键词包含——栏目名出现在正文/其它位置不误伤。
_COLUMN_PREFIXES: tuple[str, ...] = ("触乐怪话", "触乐夜话")

# 源站自营内容（2026-10-25 用户定稿「网站的自营信息我们不要」）：媒体自己办的活动、
# 自家周边、自家会员体系。**按源分表**而不是塞进总词表——「核聚变」是机核的自办展会
# 品牌，别的源报道它时说的是同一件事，混进总词表会把「别的源报道机核活动」一起挡掉。
SOURCE_SELF_OP_WORDS: dict[str, tuple[str, ...]] = {
    "gcores": (
        "核聚变", "核电波", "核聚变游戏节", "核市奇谭", "ggg", "机核商城", "机核会员",
    ),
}

# 周报 / 一周综述栏目：标题就是把一周新闻堆在一起，**没有单一事件**——
# 而且堆砌里提到的作品会被当成官宣：触乐「本周行业大事：…《塞尔达传说》重制版公开
# 新预告…」曾因含「重制版」被判成官宣、豁免入库（2026-10-25 修）。
_WEEKLY_DIGEST_WORDS: tuple[str, ...] = (
    "本周行业大事", "本周业内", "一周行业", "本周资讯", "一周资讯", "本周要闻",
    "一周要闻", "本周新闻", "一周新闻", "本周回顾", "一周回顾", "本周大事",
    "新闻综述", "周报", "週刊まとめ", "週間まとめ", "1週間のニュース",
)

# 下面两条规则的作用域（2026-10-25 用户定稿）：只对**媒体源的「游戏」类**生效。
# 官方 PR（作品方自己发的活动 / 纪念公告）与动画 / 漫画 / 轻小说 / 综合动态的判定
# 完全不变。值与 news_classify 一致，这里刻意不 import 它（本模块不依赖其它脚本）。
_MEDIA_RULE_SOURCE_TYPES: tuple[str, ...] = ("媒体编译",)
_MEDIA_RULE_CATEGORY = "游戏"

# 明确的广告 / 赞助标记：只收「媒体自己标出来的」，不猜。
HARD_AD_KEYWORDS: tuple[str, ...] = (
    "推广", "赞助", "广告", "商务合作", "软文",
    "広告", "タイアップ", "スポンサー", "ステマ", "アフィリエイト",
    "sponsored", "advertorial", "【pr】",
)

# 明确非 ACGN 的**主体类别**（不是品牌名单）：实写影视、消费电子 / AI。
# 命中这些且全文没有任何 ACGN 信号时才排除——「アニメ化決定」带「実写」也是 ACGN 新闻。
_LIVE_ACTION_WORDS: tuple[str, ...] = (
    "実写ドラマ", "実写映画", "連続ドラマ", "実写化", "実写版", "実写舞台",
    "电视剧", "网剧", "真人剧",
)
_TECH_WORDS: tuple[str, ...] = (
    "苹果", "折叠屏", "折叠手机", "iphone", "ipad", "华为", "小米", "特斯拉",
    "芯片", "处理器", "大模型", "chatgpt", "新能源汽车", "智能座舱", "vision pro",
)

# VOD 导流：只告诉你哪家平台能看，没有任何作品动态信息（还常混进实写剧）。
# 要「見放題」+ 导流词同时出现，官方「見放題配信開始」不含导流词，不会误伤。
_VOD_AVAILABILITY: tuple[str, ...] = ("見放題", "租赁", "レンタル")
_VOD_FUNNEL: tuple[str, ...] = ("どこで見れ", "サブスク", "配信先", "比較", "見るなら")

# 抽奖：要「奖品」+「参与动作」同时在标题里才算（摘要里的「チケット抽選販売」是正经资讯）。
# 中文词是 2026-10-24 补的：机核「【会员抽奖】…」这类自家会员福利稿当时漏进来
# （原先只认日语词，中文标题一条都拦不住）。「会员」收进动作词是因为它和「抽奖」
# 同现时几乎必然是引导去站方会员体系，不会误伤作品资讯。
_GIVEAWAY_PRIZE: tuple[str, ...] = (
    "抽選", "プレゼント", "当たる", "当選", "懸賞",
    "抽奖", "中奖", "福利",
)
_GIVEAWAY_ACTION: tuple[str, ...] = (
    "リポスト", "フォロー", "応募", "キャンペーン", "拡散",
    "会员", "参与", "领取", "关注",
)

# ---------- 第二层：内容意图判定 ----------

# ACGN 正向信号：作品、创作者、音乐文化、行业。用于「非 ACGN 主体」的兜底判断
# （有这些信号就不算跑题），**不作为收录门槛**——不命中不代表无关。
_ACGN_SIGNALS: tuple[str, ...] = (
    # 作品形态
    "アニメ", "动画", "动漫", "漫画", "コミック", "単行本", "連載", "週刊", "ジャンプ",
    "ライトノベル", "ラノベ", "ノベル", "小説", "小说", "ゲーム", "游戏", "手游",
    "劇場", "映画", "pv", "放送", "配信", "主題歌", "op", "ed", "キャラクター",
    # 创作者 / 制作
    "声優", "声优", "キャスト", "スタッフ", "監督", "导演", "脚本", "编剧", "漫画家",
    "作家", "原作者", "制作", "制作公司", "作画", "イラストレーター", "プロデューサー",
    "作曲家", "编曲", "歌手", "バンド", "ボカロ", "音楽", "音乐", "ライブ", "ツアー",
    "新曲", "アルバム", "乐曲", "演唱",
    # 游戏内容
    "ガチャ", "シナリオ", "実装", "アップデート", "バージョン", "ver.", "dlc",
    "スチーム", "steam", "プレイステーション", "nintendo", "switch",
    # 行业
    "業界", "业界", "訴訟", "诉讼", "倒産", "收购", "統合", "停服", "終了", "訃報",
)

# 实物商品：联名落地成「东西」时的形态。用于识别「这条资讯在卖货」。
_PHYSICAL_GOODS: tuple[str, ...] = (
    "ドーナツ", "どんぶり", "丼", "バッグ", "ポーチ", "チャーム", "ネックレス",
    "ピアス", "指輪", "ブレスレット", "ぬいぐるみ", "フィギュア", "キーホルダー",
    "プライズ", "一番くじ", "食玩", "グッズ", "雑貨", "tシャツ", "靴下", "マグ",
    "マグカップ", "クッション", "タオル", "ポスター", "ポストカード", "ステッカー",
    "下敷き", "コースター", "缶", "アクセサリー", "アクセ", "ランチョン", "カードセット",
    "まんじゅう", "饅頭", "スイーツ", "ドリンク", "コラボメニュー", "セットメニュー",
    "オーナメント", "アイテム", "コレクション", "キーホルダー",
)

# 外部商业渠道：餐饮连锁 / 服饰连锁 / 便利店 / 家电量贩 / 电商。
# 这是**封闭的类别清单**（主流连锁），不随个案扩充；出现清单外的新渠道走第三层人工复核，
# 而不是往表里加名字。
# 刻意不收食品饮料品牌本身（明治 / 森永 / ロッテ / コカ・コーラ）：它们大量出现在
# 角色名与作品名里（「中野明治」），误判成本远高于收益；也不收两字母缩写（gu 会误匹配）。
_COMMERCIAL_CHANNELS: tuple[str, ...] = (
    # 餐饮连锁
    "ミスタードーナツ", "ミスド", "吉野家", "すき家", "モスバーガー", "マクドナルド",
    "ロッテリア", "ケンタッキー", "スターバックス", "スタバ", "コメダ", "不二家",
    "サンマー", "磯丸水産", "鳥貴族",
    # 服饰 / 生活连锁
    "ユニクロ", "アベイル", "しまむら", "サンキューマート", "zara", "h&m",
    "無印良品", "ジェラートピケ", "パルコ", "ロフト", "東急ハンズ", "ドン・キホーテ",
    # 便利店 / 量贩 / 电商
    "ローソン", "ファミリーマート", "セブン-イレブン", "セブンイレブン", "イオン",
    "ビックカメラ", "ヨドバシ", "アマゾン", " amazon", "楽天", "ヤフーショッピング",
)

# 购买 / 参与导向的行动词：用户要被要求花钱、预约、到店、申请。
# 「登場 / ラインナップ」单独出现时不算购买信号（新キャラ登場是作品动态），
# 但和实物商品 / 商业渠道一起出现就是商品上架——游戏内信号会把它救回去（见下方规则）。
_PURCHASE_ACTIONS: tuple[str, ...] = (
    "予約", "预约", "販売", "发售", "开售", "発売", "上架", "購入", "购买", "先着",
    "数量限定", "受注", "申込", "クーポン", "优惠券", "特典", "もらえる", "もらえ",
    "当たる", "応募", "抽選", "スタート", "開始", "开幕", "登場", "ラインナップ",
    "発売中", "販売中", "予約受付",
)

# 店铺 / 落地场景：商品「在哪能拿到」。和实物商品一起出现即购买导流
# （例：「ぬいぐるみやダイナー衣装グッズ揃う期間限定ショップ開催」没有预约词，但有店铺）。
_STORE_WORDS: tuple[str, ...] = (
    "ショップ", "店舗", "開催", "ポップアップ", "POP UP", "pop-up", "受注販売",
    "特別企画", "キャンペーン",
)

# 观影特典（入場者特典 / 来場者特典）：看电影送的，是作品上映动态，不是卖周边。
# 只收「观影」前缀——「購入特典 / 予約特典」仍算购买附赠，照常排除（不靠「特典」单字误判）。
_THEATRICAL_BONUS: tuple[str, ...] = (
    "入場者特典", "来場者特典", "劇場来場者特典", "上映記念入場者特典", "劇場特典",
)

# 游戏内信号：内容发生在游戏里，与「去外面买东西」相对。
# 刻意只收**游戏专属**词：「開催 / イベント / 記念 / 周年 / プレゼント」这类词商业campaign
# 也在用（ポケモン×ミスド 就是「キャンペーン開催」），收进来会把联名促销误判成游戏内容。
_INGAME_SIGNALS: tuple[str, ...] = (
    "ガチャ", "シナリオ", "★", "実装", "ピックアップ", "ログインボーナス",
    "ログイン", "ボーナス", "ステージ", "バトル", "覚醒", "編成",
    "無料配布", "配布", "復刻", "コラボイベント", "新イベント",
    "アップデート", "バージョン", "ver.", "新キャラ", "プレイアブル", "クエスト",
)


def _hits(words: tuple[str, ...], text: str) -> list[str]:
    return [word for word in words if word.lower() in text]


# ---------- 综合资讯放行分组（2026-10-23 用户定稿，**后续会细化，改这里就行**） ----------
#
# 背景：综合动态里有一批「不是动画/漫画/游戏本体，但 ACGN 用户会看」的主体——日系音乐
# （anisong / J-POP）、日系偶像、VTuber。它们默认被当成「普通综合稿」，一旦带唱片 / 周边
# 发售信息就会被商业促销层拒掉（唱片发售正是这类资讯的常态）。
#
# 结构刻意做成**纯数据表**，方便以后按组细化，不需要改判定流程：
#   1. 新增一类主体：在 `COMPREHENSIVE_ALLOW_GROUPS` 里加一组 key → 信号词；
#   2. 收紧某一组：给 `_COMPREHENSIVE_GROUP_BLOCKERS` 配上该组的排除词（命中则该组不生效）；
#   3. 临时关掉某一组：改环境变量 `NEWS_COMPREHENSIVE_ALLOW_GROUPS`（删掉对应 key），不用动代码；
#   4. 组名随 `COMPREHENSIVE_ALLOW_GROUPS` 的 key 走，配置里写了不存在的 key 会被忽略（不报错）。
#
# 判定口径：**命中任一启用分组的信号词即放行**，且不再走后面的「非 ACGN 主体 / 商业促销」层；
# 硬广（媒体自标的推广 / 赞助）仍然优先排除。
# 分组顺序即优先级：具体的组（anisong / idol / vtb）排在泛音乐组（jmusic）之前，
# 否则「偶像发单曲」会先命中 jmusic 的「シングル」，reason 记成日音而不是日偶。
COMPREHENSIVE_ALLOW_GROUPS: dict[str, tuple[str, ...]] = {
    # 日系音乐：anisong / 主题歌 / 声优歌手
    "anisong": (
        "アニソン", "アニメソング", "主題歌", "エンディングテーマ", "オープニングテーマ",
        "挿入歌", "キャラソン", "声優アーティスト", "アニメMV", "オーケストラコンサート",
    ),
    # 日系偶像：团体 / 坂道 / 事务所 / 握手会等偶像活动
    "idol": (
        "アイドル", "乃木坂", "櫻坂", "日向坂", "akb48", "ジャニーズ", "snow man",
        "坂道", "握手会", "卒業公演", "生誕祭", "アイドルグループ",
    ),
    # VTuber / Vシンガー：事务所、直播、翻唱、3D 披露
    "vtb": (
        "vtuber", "vシンガー", "バーチャルyoutuber", "バーチャルライバー",
        "ホロライブ", "にじさんじ", "vspo", "歌い手", "3dライブ", "初配信",
    ),
    # 日系音乐：单曲 / 专辑 / 巡演 / 榜单（不绑定动画的作品向音乐动态）
    "jmusic": (
        "邦楽", "j-pop", "jpop", "シングル", "アルバム", "ミニアルバム", "ワンマン",
        "ライブツアー", "全国ツアー", "フェス", "配信リリース", "オリコン", "ビルボード",
        "music video", "ミュージックビデオ",
    ),
}

# 分组级排除词：命中则该分组本次不生效（留给以后细化，例如「偶像团体带货」「VTuber 的
# 商业代言」需要挡掉时，把词加到对应组即可；不改判定流程、不影响其它组）。
# key 必须存在于 COMPREHENSIVE_ALLOW_GROUPS；空元组表示该组暂无排除。
_COMPREHENSIVE_GROUP_BLOCKERS: dict[str, tuple[str, ...]] = {
    "anisong": (),
    "jmusic": (),
    "idol": (),
    "vtb": (),
}

# 「综合动态」的取值：以 news_classify.SUBJECT_GENERAL 为准，历史别名「综合」一并认。
_COMPREHENSIVE_CATEGORIES: tuple[str, ...] = ("综合动态", "综合")


def enabled_comprehensive_groups() -> tuple[str, ...]:
    """当前生效的分组 key（来自 `NEWS_COMPREHENSIVE_ALLOW_GROUPS`，顺序即优先级）。

    配置里出现表里没有的 key 直接忽略——避免拼错一个组名把整条放行规则关掉。
    """
    from app.config import settings  # 局部导入：本模块顶层保持零依赖

    raw = (settings.news_comprehensive_allow_groups or "").split(",")
    wanted = [key.strip() for key in raw if key.strip()]
    return tuple(key for key in wanted if key in COMPREHENSIVE_ALLOW_GROUPS)


def comprehensive_allow_group(
    title: str, summary: str = "", *, groups: Iterable[str] | None = None
) -> str:
    """命中哪个放行分组（返回组名，未命中返回空串）。

    只做「信号词命中 + 该组排除词未命中」，不判断其它层——调用方决定怎么用。
    """
    text = f"{title or ''}\n{summary or ''}".lower()
    for key in groups if groups is not None else enabled_comprehensive_groups():
        words = COMPREHENSIVE_ALLOW_GROUPS.get(key)
        if not words or not _hits(words, text):
            continue
        if _hits(_COMPREHENSIVE_GROUP_BLOCKERS.get(key, ()), text):
            continue
        return key
    return ""


# ---------- 复制粘贴的通稿引言（2026-10-24 用户定稿） ----------
#
# 厂商发给媒体的参展 / 联名通稿都自带同一段活动引言，一批里能占掉整个源的一大半
# （2026-10-24 实测：机核 feed 20 条里 8 条摘要同是「核聚变游戏嘉年华北京站即将开幕，
# 热门新游试玩、精彩舞台活动、限定周边收集…」）。收录它们等于把站方自办展会的招商稿
# 搬过来，跟「站方自有节目」是同一类噪音。
#
# 判据是**同批内摘要前缀重复**，刻意**不认具体展会名**：明年换个展会、换个源照样
# 收敛，不用维护词表。正常资讯的摘要前 40 字不会撞车（各自的导语不一样）。

REPEAT_SUMMARY_PREFIX = 40  # 比较摘要前多少个字符
REPEAT_SUMMARY_MIN_LEN = 20  # 摘要短于此不参与比较（短摘要前缀相同往往只是「官方宣布」）

_NON_WORD_RE = re.compile(r"[\s\W_]+")


def summary_prefix_key(summary: str, size: int = REPEAT_SUMMARY_PREFIX) -> str:
    """摘要指纹：去掉空白与标点后的前 ``size`` 个字符（用于同批去重）。"""
    return _NON_WORD_RE.sub("", summary or "")[:size]


def has_acgn_signal(title: str, summary: str) -> bool:
    """全文有没有 ACGN 正向信号（作品 / 创作者 / 音乐文化 / 行业）。

    只用于「非 ACGN 主体」的兜底：没有任何信号又带实写 / 科技信号才算跑题。
    **不作为收录门槛**——音乐人动态、行业新闻常不带这些词。
    """
    text = f"{title}\n{summary}".lower()
    return bool(_hits(_ACGN_SIGNALS, text))


def _self_op_word_in_text(word: str, text: str) -> bool:
    """自营词命中：ASCII 词**只要求左侧边界**，右边直接接中文也算命中
    （「GGG完整曲目与阵容公开」「gpass会员」是常态写法）。

    不能用 ``_term_in_text``（它两侧都要边界）：右侧挡汉字的规则是为词表里的
    ``tga`` 那种字母偶然组合设计的，自营品牌名反过来常贴着中文写，会被漏掉。
    """
    needle = (word or "").lower()
    if not needle or needle not in text:
        return False
    if all(ord(ch) < 128 for ch in needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}", text) is not None
    return True


def self_op_reason(source_key: str, title: str, summary: str) -> str | None:
    """源站自营内容（媒体自办活动 / 自家周边 / 自家会员栏目）→ 跳过原因。

    按来源键在 ``SOURCE_SELF_OP_WORDS`` 里取词表（2026-10-25 用户「网站的自营信息
    我们不要」）。别的源报道同一个活动不算自营——分源词表就是为了这个。
    """
    text = f"{title}\n{summary}".lower()
    for word in SOURCE_SELF_OP_WORDS.get((source_key or "").strip(), ()):
        if _self_op_word_in_text(word, text):
            return f"源站自营内容：{word}"
    return None


def weekly_digest_reason(title: str, summary: str) -> str | None:
    """周报 / 一周综述栏目 → 跳过原因；不是周报返回 None。

    命中就拒，不看它里面提到了哪些作品：那些作品名会让周报被判成官宣走豁免
    （触乐「本周行业大事」就是这样混进来的）。
    """
    text = f"{title}\n{summary}".lower()
    for word in _WEEKLY_DIGEST_WORDS:
        if word.lower() in text:
            return f"周报 / 行业综述栏目：{word}"
    return None


def media_game_rule_reason(
    title: str,
    summary: str,
    category: str = "",
    source_type: str = "",
    terms: Iterable[str] = (),
) -> str | None:
    """媒体源「游戏」类专项（2026-10-25 用户定稿）：周报栏目 + 普通游戏的联名周边。

    只有「媒体源的遊戲類」才判，其余一律返回 None——官方 PR 的活动 / 纪念公告与
    动画 / 漫画 / 轻小说 / 综合动态的既有判定完全不受影响。
    对外暴露是为了让存量清理脚本与抓取链路共用同一份口径（规则改一处两边同时生效）。
    """
    if (category or "").strip() != _MEDIA_RULE_CATEGORY:
        return None
    if (source_type or "").strip() not in _MEDIA_RULE_SOURCE_TYPES:
        return None
    return weekly_digest_reason(title, summary) or collab_merch_reason(
        title, summary, terms
    )


def collab_merch_reason(
    title: str, summary: str, terms: Iterable[str] = ()
) -> str | None:
    """普通游戏的联名周边 → 跳过原因；ACG IP 的联名不拒。

    判据三件齐备才拒：联名标记 × 实物形态 × **没有 ACG 证据**。
    ACG 证据 = 站内 ACGN 游戏词表（作品名 / 别名 / 系列 / 角色 / 创作者）命中，
    或 ``_COLLAB_ACG_SIGNALS`` 的强体裁信号命中。**不认「角色」这类泛词**
    （普通游戏文案里到处都是，用它当证据等于这条规则失效）。
    ``terms`` 为空时（没加载词表）只剩体裁信号，宁可多放不多拒。
    """
    text = f"{title}\n{summary}".lower()
    mark = _hits(_COLLAB_MERCH_MARKS, text)
    if not mark:
        return None
    goods = _hits(_COLLAB_MERCH_GOODS, text) or _hits(_PHYSICAL_GOODS, text)
    if not goods:
        return None
    if _hits(_COLLAB_ACG_SIGNALS, text) or _first_term_hit(terms, text):
        return None
    return f"普通游戏的联名周边（{mark[0]} × {goods[0]}）"


def review_reason(title: str, summary: str) -> str | None:
    """第三层：低置信度 → 返回待复核原因（不丢弃，由调用方照常入库并标记）。

    三种情形：
    1. 有商业渠道但看不出在卖实物（可能是店内活动 / 展示 / 联名内容）；
    2. 只见实物商品，没有购买 / 渠道 / 店铺信号（可能是商品化公告的开头）；
    3. 游戏内信号与实物商品信号冲突（可能是买游戏内道具，也可能是卖周边）。
    """
    text = f"{title}\n{summary}".lower()
    goods = _hits(_PHYSICAL_GOODS, text)
    channels = _hits(_COMMERCIAL_CHANNELS, text)
    if not goods:
        if channels:
            return f"商业渠道信号但未见实物商品（可能是店内活动）：{channels[0]}"
        return None

    actions = _hits(_PURCHASE_ACTIONS, text)
    stores = _hits(_STORE_WORDS, text)
    ingame = _hits(_INGAME_SIGNALS, text)
    if ingame and (actions or stores):
        return f"游戏内信号与实物商品信号冲突：{ingame[0]} / {goods[0]}"
    if not (actions or channels or stores):
        return f"只见实物商品、无购买/渠道/店铺信号（可能是商品化公告）：{goods[0]}"
    return None


def should_skip_article(
    title: str,
    summary: str,
    category: str = "",
    source_type: str = "",
    source_label: str = "",
    url: str = "",
    source_key: str = "",
    game_terms: Iterable[str] = (),
) -> str | None:
    """返回跳过原因（命中则跳过该条），未命中返回 None。

    分层顺序：**站方自有节目**（链接形态 / 内容信号）→ 源站自营内容 → 栏目闲聊 → 硬广
    → **媒体源游戏专项**（周报 / 普通游戏的联名周边）→ **综合放行分组** → 非 ACGN 主体
    → VOD 导流 → 抽奖 → 商业促销（第二层）。
    任何一层都不做「单个关键词定生死」：都要两个不同性质的信号同时出现
    （站方自有节目与自营内容除外——它们判的是「这条根本不是给外人看的资讯」，
    形态确定即排除）。

    ``category`` / ``source_type`` 用于媒体源游戏专项（周报 / 联名周边，只认媒体源的
    游戏类）；``category`` 另用于「综合放行分组」。``source_key`` 用于源站自营内容
    的分源词表。``game_terms`` 是站内 ACGN 游戏词表（联名周边的 ACG 证据来源），
    传空元组时该判据只剩体裁信号。``source_label`` 保留签名兼容，**不参与判定**
    （来源白名单与来源类型门槛 2026-10-21 已移除）。
    ``url`` 只用于站方自有节目的链接形态判定，空串时该判据跳过。
    """
    del source_label  # 显式声明不使用，避免误以为漏传
    headline = (title or "").lower()
    text = f"{title}\n{summary}".lower()
    link = (url or "").lower()

    # 第零层：站方自有节目 / 编辑部内容（不是资讯，判据最确定，放在最前）
    for marker in _SOURCE_PROGRAM_URL_MARKERS:
        if marker in link:
            return f"站方自有节目（非资讯，链接形态 {marker}）"
    for word in _SOURCE_PROGRAM_WORDS:
        if _term_in_text(word, text):
            return f"站方自有节目（非资讯）：{word}"

    # 第零层：源站自营内容（媒体自办活动 / 自家周边 / 自家会员），按源分表。
    # 与「站方自有节目」同类：判的是「这条不是资讯」，形态确定即排除。
    self_op = self_op_reason(source_key, title, summary)
    if self_op:
        return self_op

    # 第一层：固定栏目闲聊（标题前缀，最确定）
    for prefix in _COLUMN_PREFIXES:
        if (title or "").strip().startswith(prefix):
            return f"栏目闲聊（{prefix}）：编辑随笔，无资讯量"

    # 第一层：硬广标记（媒体自己标的，最确定）
    # 放在放行分组之前：赞助稿即使聊的是 VTuber / 偶像也照排。
    for keyword in HARD_AD_KEYWORDS:
        if keyword.lower() in text:
            return f"硬广标记：{keyword}"

    media_reason = media_game_rule_reason(
        title, summary, category, source_type, game_terms
    )
    if media_reason:
        return media_reason

    # 综合放行分组（2026-10-23）：日系音乐 / 日偶 / VTuber 属于 ACGN 邻接内容，
    # 在「综合动态」里放行，且不再走后面的非 ACGN 与商业促销层——唱片 / 周边发售
    # 是这类资讯的常态，按促销拒掉会把这些主体整体挡在站外。
    # 规则表见 COMPREHENSIVE_ALLOW_GROUPS（后续细化只改那张表 / 那个配置项）。
    if (category or "").strip() in _COMPREHENSIVE_CATEGORIES:
        if comprehensive_allow_group(title, summary):
            return None


    # 第一层：明确非 ACGN 主体（实写 / 科技）——必须同时没有 ACGN 信号
    if not has_acgn_signal(title, summary):
        for word in _LIVE_ACTION_WORDS:
            if word.lower() in text:
                return f"非 ACGN 主体（实写影视）：{word}"
        for word in _TECH_WORDS:
            if word.lower() in text:
                return f"非 ACGN 主体（消费电子/科技）：{word}"

    # 第一层：VOD 导流（要「能看」+「去哪看 / 比一比」两个信号）
    vod = _hits(_VOD_AVAILABILITY, headline)
    funnel = _hits(_VOD_FUNNEL, headline)
    if vod and funnel:
        return f"VOD 导流稿（无作品动态信息）：{vod[0]} + {funnel[0]}"

    # 第一层：抽奖（奖品 + 参与动作，都要在标题里）
    prize = _hits(_GIVEAWAY_PRIZE, headline)
    action = _hits(_GIVEAWAY_ACTION, headline)
    if prize and action:
        return f"抽奖导流（要用户参与转发/应募）：{prize[0]} + {action[0]}"

    # 第二层：商业促销——看「主要内容是不是在卖实物、要去哪拿」。
    # 判据是**实物商品 + （购买行动 或 商业渠道 或 店铺场景）**：只出现实物词但看不出
    # 怎么买（例：「缶セット」单独出现）走第三层待复核，不直接排除。
    goods = _hits(_PHYSICAL_GOODS, text)
    actions = _hits(_PURCHASE_ACTIONS, text)
    channels = _hits(_COMMERCIAL_CHANNELS, text)
    stores = _hits(_STORE_WORDS, text)
    ingame = _hits(_INGAME_SIGNALS, text)

    # 观影特典：看电影送的（入場者特典），是作品上映动态 → 保留，不按卖周边排除（修 #211 误伤）。
    if _hits(_THEATRICAL_BONUS, text):
        return None

    if goods and (actions or channels or stores) and not ingame:
        anchor = goods[0]
        with_what = (actions or channels or stores)[0]
        return f"商业联名/促销（主要内容是购买或预约）：{anchor} + {with_what}"
    return None


# ---------- 游戏类准入闸门（2026-10-23 用户定稿） ----------
#
# 和本模块「不靠名单打地鼠」的原则不冲突，边界写清楚：
# - 主体是否 ACGN 仍由 should_skip_article 按内容语义判，本闸门不管；
# - 本闸门只回答「这条**游戏**资讯符不符合站点定位」，**放行完全依赖实体词表**
#   （站内 ACGN 系游戏的作品名 / 别名 / 系列 / 角色 / 创作者，见 crud.list_acgn_game_terms）；
# - 名单只用于**排除**，且是封闭清单（主流欧美 3A 与大厂 + 硬件外设术语），不随个案扩充：
#   漏掉的 3A 会因为不在 ACGN 实体词表里，被第 4 步兜底拒掉，不依赖名单完整。

_GAME_HARDWARE_WORDS: tuple[str, ...] = (
    "rtx", "geforce", "radeon", "gpu", "グラフィックスボード", "グラボ", "ドライバ",
    "ベンチマーク", "フレームレート", "リフレッシュレート", "解像度", "マウス",
    "キーボード", "ヘッドセット", "ヘッドホン", "イヤホン", "モニター", "ディスプレイ",
    "マザーボード", "cpu", "ssd", "メモリ", "冷却ファン", "ゲーミングデバイス",
    "ゲーミングpc", "外付けhdd",
)

_GAME_COLLAB_WORDS: tuple[str, ...] = (
    "コラボ", "コラボモデル", "コラボレーション", "限定モデル", "限定デザイン",
    "コラボアイテム", "コラボグッズ", "联名", "聯名", "联动款", "限定款",
)

# 欧美 3A / 大厂：只用于排除。日厂作品（バイオハザード / エルデンリング 等）刻意不收——
# 它们有动画 / 电影改编，属于站内该收的范围。
_GAME_AAA_WORDS: tuple[str, ...] = (
    "call of duty", "battlefield", "assassin", "grand theft auto", "gta",
    "fifa", "forza", "halo", "god of war", "the last of us", "uncharted",
    "red dead", "far cry", "watch dogs", "apex legends", "overwatch",
    "diablo", "world of warcraft", "starcraft", "need for speed",
    "electronic arts", "ea sports", "ubisoft", "activision", "blizzard",
    "rockstar", "bethesda", "cd projekt", "epic games",
)

# ACGN 正向信号：命中就不按「普通游戏」处理（改编 / 二次元属性 / 创作者）。
_GAME_ACG_SIGNALS: tuple[str, ...] = (
    "アニメ", "动画", "アニメ化", "実写化", "舞台化", "コミカライズ", "ノベライズ",
    "ゲーム化", "声優", "声优", "キャラクター", "角色", "美少女", "乙女", "ギャルゲー",
    "ガチャ", "ビジュアルノベル", "ノベルゲー", "恋愛", "恋爱", "アイドル", "偶像",
    "エッジランナーズ", "スピンオフ", "外伝",
    # 魔法少女是 ACGN 体裁词本身（2026-10-23 用户点名）：标题里出现就说明是二次元向，
    # 不依赖作品是否在站内作品库里——「新作魔法少女ゲーム」这种稿子以前会被兜底拒掉。
    "魔法少女", "変身ヒロイン", "魔法少女もの",
)

# 同人游戏（2026-10-23 用户要求「考虑放行可能性」→ 放行）：同人即 ACGN 原生形态，
# 站内作品库本来就有 `同人` 标签（60 部），这里补的是「作品还没进库」的新作报道。
# 成人向内容不在这层拦——`news_classify.flag_adult` 会打 is_adult，由展示层开关遮蔽。
#
# **商业独立游戏不在放行范围**（2026-10-23 用户更正）：`インディー / indie / 個人開発 /
# アーリーアクセス / early access / itch.io` 这些形态词一律不作为放行依据，
# 「インディーFPS」「インディー展示会」这类就是普通游戏稿，走兜底拒。
_GAME_DOUJIN_SIGNALS: tuple[str, ...] = (
    "同人ゲーム", "同人サークル", "同人ソフト", "同人誌即売会", "コミティア",
    "コミケ", "東方project", "同人作品", "二次創作ゲーム",
)

# 体裁闸：命中这些就不是「独立/同人」能豁免的对象，仍按普通游戏拒。
# 只收「真实系 / 竞技系」这类明确偏离本站定位的体裁，不收 RPG / ADV / パズル。
_GAME_GENERIC_COMPETITIVE: tuple[str, ...] = (
    "fps", "バトルロイヤル", "battle royale", "デスマッチ", "team deathmatch",
    "レーシング", "レースゲーム", "racing", "eスポーツ", "esports",
    "スポーツゲーム", "サッカー", "アメフト", "ゴルフ", "ボウリング",
    "パチンコ", "パチスロ",
)

# 年度评奖：欧美 3A 只有这一类放行（2026-10-23 用户定稿「只放 TGA 之类」）。
# 刻意**不含**展会（E3 / TGS / gamescom 开展）——展会稿噪音大，容易把 3A 又放回来。
_GAME_AWARD_WORDS: tuple[str, ...] = (
    "tga", "the game awards", "goty", "game of the year", "年度游戏", "年度最佳",
    "日本ゲーム大賞", "japan game awards", "gamescom award", "dice awards",
    "bafta games", "golden joystick", "cedec awards",
)

# 游戏子类里天然属于 ACGN 的两类（news_classify.GAME_SUBCATS 的子集）。
_GAME_ACG_SUBCATEGORIES: tuple[str, ...] = ("Galgame", "二游")

# 硬件 × IP 联名周边：收，但降分（与商品宣传同档），排在正常资讯后面。
_GAME_COLLAB_PENALTY = -30

# 普通游戏的联名周边（2026-10-25 用户「普通游戏的联名不在乎」）：联名 / 纪念款
# 落地成实物时，被联名的必须是 ACG IP——普通游戏（欧美 3A、大众向作品）的联名手柄、
# 联名键盘这类周边不收。判据与游戏闸门第 1 步同款：
# 「联名标记 × 实物 × 没有 ACG 证据」，三条齐了才拒。
_COLLAB_MERCH_MARKS: tuple[str, ...] = (
    "联名", "聯名", "联动款", "限定款", "纪念款", "コラボ", "コラボレーション",
)
# 联名周边的 ACG 证据只看**强体裁信号**：刻意不收「角色 / キャラクター / 动画 /
# 恋愛 / アイドル」这类泛词——普通游戏的文案里同样满地都是（八位堂 × 古墓丽影
# 联名手柄的摘要写着「她不是游戏里一个虚拟的角色」，用泛词当证据会让普通游戏的
# 联名全部漏网）。真正的依据是站内 ACGN 游戏词表，体裁信号只救「作品还没进库」的。
_COLLAB_ACG_SIGNALS: tuple[str, ...] = (
    "ギャルゲー", "ビジュアルノベル", "ノベルゲー", "乙女", "美少女", "魔法少女",
    "変身ヒロイン",
    # 有动画 / 漫画 / 小说改编关系的作品（古墓丽影这类欧美 3A 没有）
    "アニメ化", "アニメ", "动画", "ノベライズ", "コミカライズ", "ゲーム化",
)

# 外设类形态词：_PHYSICAL_GOODS 收的是服饰 / 日用品 / 食玩，没有这些。
_COLLAB_MERCH_GOODS: tuple[str, ...] = (
    "手柄", "键盘", "鼠标", "鼠标垫", "耳机", "机箱", "游戏手柄",
    "コントローラー", "ゲームパッド", "キーボード", "マウスパッド",
    # 中文周边通称（_PHYSICAL_GOODS 里只有日文的 グッズ / 雑貨）
    "周边", "手办", "公仔", "抱枕", "立牌", "徽章", "挂件", "联名款",
)


@dataclass(frozen=True)
class GameAdmission:
    """游戏准入结论：allowed=False 时 reason 是拒绝原因；allowed 且 penalty<0 表示降分。"""

    allowed: bool
    reason: str = ""
    penalty: int = 0


def _term_in_text(term: str, text: str) -> bool:
    """词条是否出现在文本里。

    ASCII 词条要求「词边界」：前后不能是字母 / 数字 / 假名 / 汉字，只允许标点、
    空白与字符串边界（数字例外，日文常写「DOA6」「TGA2026」）。
    只挡字母是不够的——日文文本里 ``のtga続き`` 会被误判成命中（回放时真发生过）。
    """
    needle = term.lower()
    haystack = (text or "").lower()
    if not needle or needle not in haystack:
        return False
    if all(ord(ch) < 128 for ch in needle):
        pattern = rf"(?<![a-z0-9\u3040-\u30ff\u4e00-\u9fff]){re.escape(needle)}(?![a-z\u3040-\u30ff\u4e00-\u9fff])"
        return re.search(pattern, haystack) is not None
    return True


def _first_term_hit(terms: Iterable[str], text: str) -> str:
    for term in terms:
        if _term_in_text(term, text):
            return term
    return ""


def _hits_bounded(words: tuple[str, ...], text: str) -> list[str]:
    """同 ``_hits``，但 ASCII 词条要求词边界。

    评奖 / 硬件 / 3A 三张表必须用它：``tga`` 用纯子串匹配会命中任何含这三个字母
    的单词（回放时把一条「巨大怪物リヴァイアサン」的普通游戏稿判成了年度评奖）。
    """
    return [word for word in words if _term_in_text(word, text)]


def judge_game_admission(
    title: str = "",
    summary: str = "",
    subcategory: str = "",
    terms: Iterable[str] = (),
) -> GameAdmission:
    """游戏类准入判定（只对 category == 游戏、且来源在 NEWS_GAME_FILTER_SOURCES 时调用）。

    顺序（2026-10-23 用户定稿，顺序本身就是规则）：
    0. 年度评奖 → 放行（TGA / GOTY / 日本ゲーム大賞 等，不含展会）；
    1. 硬件外设 → 有 IP 联名证据才收（降 30 分），否则拒；
       **硬件必须先判**：否则「GeForce 驱动稿里提了一句宝可梦」会命中 IP 直接放行；
    2. 欧美 3A / 大厂 → 无 ACGN 信号即拒（Cyberpunk × 动画联动这类会被信号救回）；
    3. ACGN 白名单：子类 Galgame / 二游 → ACG 信号（含魔法少女等体裁词）→
       实体词表（作品/别名/系列/角色/创作者）；
    4. 同人游戏 → 放行，但命中真实系竞技体裁（FPS / 竞速 / 体育 / 电竞）则拒；
    5. 以上都不命中 → 拒（普通游戏稿）。

    第 4 步只认**同人**，不认商业独立游戏：`インディー / 個人開発 / アーリーアクセス`
    这类形态词不放行（2026-10-23 用户更正「欧美独立游戏不可以」）。

    terms 由 ``crud.list_acgn_game_terms`` 提供，每轮导入加载一次后传入。
    """
    text = f"{title or ''}\n{summary or ''}".lower()

    award = _hits_bounded(_GAME_AWARD_WORDS, text)
    if award:
        return GameAdmission(True, f"年度评奖：{award[0]}")

    hardware = _hits_bounded(_GAME_HARDWARE_WORDS, text)
    if hardware:
        collab = _hits(_GAME_COLLAB_WORDS, text)
        ip_name = _first_term_hit(terms, text)
        if collab and ip_name:
            return GameAdmission(
                True, f"ACG 周边联名（降分）：{ip_name} × {collab[0]}", _GAME_COLLAB_PENALTY
            )
        return GameAdmission(False, f"硬件 / 外设稿：{hardware[0]}")

    acg_signal = _hits(_GAME_ACG_SIGNALS, text)
    aaa = _hits_bounded(_GAME_AAA_WORDS, text)
    if aaa and not acg_signal:
        return GameAdmission(False, f"欧美 3A / 泛射击竞速：{aaa[0]}")

    if subcategory in _GAME_ACG_SUBCATEGORIES:
        return GameAdmission(True, f"游戏子类：{subcategory}")
    if acg_signal:
        return GameAdmission(True, f"ACG 信号：{acg_signal[0]}")
    ip_name = _first_term_hit(terms, text)
    if ip_name:
        return GameAdmission(True, f"ACG 作品词表：{ip_name}")

    # 同人游戏：按「同人 / 同人展会」放行，但泛竞技体裁仍然拒（见 _GAME_GENERIC_COMPETITIVE）。
    # 用 `_hits`（子串）而不是 `_hits_bounded`：日文常写「同人ゲーム」，假名紧贴 ASCII，
    # 词边界会把最常见写法挡掉（`tga` 那种字母偶然组合的误判风险这张表没有）。
    doujin = _hits(_GAME_DOUJIN_SIGNALS, text)
    if doujin:
        generic = _hits(_GAME_GENERIC_COMPETITIVE, text)
        if generic:
            return GameAdmission(False, f"同人但属泛竞技体裁：{generic[0]}")
        return GameAdmission(True, f"同人游戏：{doujin[0]}")

    return GameAdmission(False, "无 ACGN 关联的普通游戏")
