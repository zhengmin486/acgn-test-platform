"""资讯分类 / 事件 / 优先级 三维度分离的定向测试。

覆盖验收要点：主体类型与游戏子类可区分、音乐与创作者动态能归入对应主体并拿到合理
优先级、日本二游与国产二游同权、欧美普通受控而重大可破格、成人内容只标记不删除。
"""

import pytest

from scripts import news_classify as nc


# ---------- 主体类型（面向用户的四类 + 综合动态兜底） ----------

def test_subject_normalization_maps_legacy_labels():
    """旧栏目名归一化到主体类型；音乐 / 业界不另立主体。"""
    assert nc.normalize_subject("动漫资讯") == nc.SUBJECT_ANIME
    assert nc.normalize_subject("综合资讯") == nc.SUBJECT_GENERAL
    assert nc.normalize_subject("活动资讯") == nc.SUBJECT_GENERAL
    assert nc.normalize_subject("动画") == nc.SUBJECT_ANIME
    assert nc.normalize_subject("漫画") == nc.SUBJECT_MANGA
    assert nc.normalize_subject("轻小说") == nc.SUBJECT_LN
    assert nc.normalize_subject("游戏") == nc.SUBJECT_GAME


def test_subject_types_has_no_music_or_industry():
    """音乐 / 创作者 / 业界 不是主体类型（只作事件类型）。"""
    assert set(nc.SUBJECT_TYPES) == {"动画", "漫画", "轻小说", "游戏", "综合动态"}


# ---------- 游戏子类 ----------

def test_game_subcategory_galge_gacha_other():
    assert nc.classify_subcategory("「魔法少女」ギャルゲー発売", "", "游戏") == nc.GAME_SUB_GALGE
    assert nc.classify_subcategory("新作ソシャゲ先行配信", "", "游戏") == nc.GAME_SUB_GACHA
    assert nc.classify_subcategory("Switch 新作を発売", "", "游戏") == nc.GAME_SUB_OTHER


def test_subcategory_only_applies_to_game():
    assert nc.classify_subcategory("ギャルゲーアニメ化決定", "", "动画") == ""


# ---------- 音乐 / 创作者动态归入对应主体，且不被沉底 ----------

def test_music_news_falls_into_related_subject_and_gets_priority():
    """动画主题曲归动画，事件类型标「歌曲发布」，优先级高于普通综合动态。"""
    event = nc.classify_event_type("夜遊（ヨルシカ）新曲「太陽」配信開始", "")
    assert event == nc.EVENT_SONG
    score = nc.compute_priority_score(
        category="动画", region=nc.REGION_JP, event_type=event,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    general = nc.compute_priority_score(
        category="综合动态", region=nc.REGION_JP, event_type=nc.EVENT_OTHER,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert score > general


def test_merch_promo_gets_lower_priority():
    """商品宣传降权：同主体下，周边/联名稿的优先级必须低于作品动态稿。"""
    work_news = _score(category="动画", text="「ベルセルク」新作アニメの制作が決定")
    merch = _score(
        category="动画",
        text="「ベルセルク」ガッツ＆グリフィスがセットでフィギュア化！",
    )
    assert merch == work_news + nc.MERCH_PROMO_PENALTY
    assert merch < work_news


def test_merch_penalty_does_not_apply_to_plain_news():
    """不命中周边词的作品动态不受降权影响。"""
    plain = _score(category="动画", text="「ベルセルク」新エピソードの配信が開始")
    assert plain == _score(category="动画")


def test_merch_penalty_still_keeps_content_above_general_bucket():
    """降权只是压一档：动画周边仍高于「综合动态」兜底桶，不会沉到看不见。"""
    merch = _score(category="动画", text="「ワンピース」新作一番くじ全ラインナップ公開")
    general = _score(category="综合动态", event_type=nc.EVENT_OTHER)
    assert merch > general


def test_creator_health_news_is_not_buried():
    """麻枝准健康状况这类重大人物消息，不因「不是新作发布」而沉底。"""
    event = nc.classify_event_type("麻枝准、体調不良により入院", "")
    assert event == nc.EVENT_HEALTH
    score = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_JP,
        event_type=event, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    # 高于同主体（日本其它游戏）的普通发售消息
    plain = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_JP,
        event_type=nc.EVENT_RELEASE, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert score > plain


# ---------- 优先级规则 ----------

def _score(**kwargs):
    kwargs.setdefault("region", nc.REGION_JP)
    kwargs.setdefault("event_type", nc.EVENT_NEW)
    kwargs.setdefault("source_type", nc.SOURCE_TYPE_MEDIA)
    return nc.compute_priority_score(**kwargs)


def test_galge_is_top_priority_within_games():
    galge = _score(category="游戏", subcategory=nc.GAME_SUB_GALGE)
    gacha = _score(category="游戏", subcategory=nc.GAME_SUB_GACHA)
    other = _score(category="游戏", subcategory=nc.GAME_SUB_OTHER)
    assert galge > gacha > other


def test_general_subject_same_weight_as_game():
    """综合动态与游戏同权（2026-10-21 用户定稿）：基础分相同，抓取配额同级竞争。"""
    general = _score(category="综合动态")
    game = _score(category="游戏", subcategory=nc.GAME_SUB_OTHER)
    assert general == game
    assert nc._SUBJECT_BASE[nc.SUBJECT_GENERAL] == nc._SUBJECT_BASE[nc.SUBJECT_GAME]


def test_galge_ranks_with_japanese_core_media():
    """日本动画 / 漫画 / 轻小说 与 Galgame 同属顶级优先（普通商品资讯不得压过）。"""
    galge = _score(category="游戏", subcategory=nc.GAME_SUB_GALGE)
    anime = _score(category="动画")
    manga = _score(category="漫画")
    ln = _score(category="轻小说")
    assert min(anime, manga, ln) >= gacha_score() and galge >= max(anime, manga, ln) - 10
    # 四者都应明显高于兜底综合动态
    general = _score(category="综合动态")
    assert min(galge, anime, manga, ln) > general


def gacha_score():
    return _score(category="游戏", subcategory=nc.GAME_SUB_GACHA)


def test_jp_and_cn_gacha_are_equal():
    """日本二游与国产二游同权（仅限真正的二次元游戏）。"""
    jp = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_GACHA, region=nc.REGION_JP,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    cn = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_GACHA, region=nc.REGION_CN,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert jp == cn


def test_cn_non_gacha_is_lower_than_jp_game():
    jp_other = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_JP,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    cn_other = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_CN,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert jp_other > cn_other


def test_western_ordinary_is_low_but_major_can_be_promoted():
    west_plain = nc.compute_priority_score(
        category="动画", region=nc.REGION_WEST, event_type=nc.EVENT_OTHER,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    west_major = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_WEST,
        event_type=nc.EVENT_INDUSTRY, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    jp_plain = nc.compute_priority_score(
        category="动画", region=nc.REGION_JP, event_type=nc.EVENT_OTHER,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert west_plain < jp_plain  # 欧美普通内容低优先级
    assert west_major > west_plain  # 重大行业事件可破格提升


def test_western_ordinary_new_release_is_not_promoted():
    """普通欧美新作发布不得破格（不能仅凭「新作发布」就当重大事件）。"""
    west_new = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_WEST,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
        text="新作ゲーム発売決定、2027年春に配信",
    )
    jp_new = nc.compute_priority_score(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_JP,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
        text="新作ゲーム発売決定、2027年春に配信",
    )
    assert west_new < jp_new


def test_western_major_signal_can_be_promoted():
    """命中公认重大信号（TGA 等）的欧美资讯才破格提升。"""
    base = dict(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_WEST,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    plain = nc.compute_priority_score(event_type=nc.EVENT_OTHER, text="海外ゲーム発売", **base)
    major = nc.compute_priority_score(
        event_type=nc.EVENT_OTHER, text="The Game Awards 年度游戏受賞", **base
    )
    assert major > plain


def test_japanese_vendor_in_western_source_is_not_boosted():
    """欧美源里的任天堂等日本厂商内容，不按「欧美重大」破格。"""
    base = dict(
        category="游戏", subcategory=nc.GAME_SUB_OTHER, region=nc.REGION_WEST,
        event_type=nc.EVENT_NEW, source_type=nc.SOURCE_TYPE_MEDIA,
    )
    nintendo = nc.compute_priority_score(text="任天堂 Switch 新作発表", **base)
    west_generic = nc.compute_priority_score(text="海外スタジオ新作発表", **base)
    assert nintendo <= west_generic


def test_pr_source_is_demoted():
    pr = nc.compute_priority_score(
        category="动画", region=nc.REGION_JP, event_type=nc.EVENT_RELEASE,
        source_type=nc.SOURCE_TYPE_PR,
    )
    media = nc.compute_priority_score(
        category="动画", region=nc.REGION_JP, event_type=nc.EVENT_RELEASE,
        source_type=nc.SOURCE_TYPE_MEDIA,
    )
    assert pr < media


# ---------- 地区只作内部权重 ----------

def test_region_detection():
    assert nc.detect_region(nc.REGION_JP, "米哈游新作発表", "") == nc.REGION_CN
    assert nc.detect_region(nc.REGION_JP, "Blizzard 新作", "") == nc.REGION_WEST
    assert nc.detect_region(nc.REGION_JP, "新作アニメ放送", "") == nc.REGION_JP


# ---------- 成人内容只标记，不删除 ----------

def test_adult_flagging_does_not_change_priority_direction():
    assert nc.flag_adult("18禁ギャルゲー発売決定", "") is True
    assert nc.flag_adult("新作アニメ放送開始", "") is False
    # galge 即便被标记为成人向，仍是游戏内最高优先级
    galge = _score(category="游戏", subcategory=nc.GAME_SUB_GALGE)
    assert galge > _score(category="游戏", subcategory=nc.GAME_SUB_GACHA)


def test_unknown_event_falls_back_to_other():
    assert nc.classify_event_type("", "") == nc.EVENT_OTHER


def test_unknown_region_scores_without_error():
    assert isinstance(_score(category="动画", region="火星"), int)
