"""评分可信度口径。

背景（2026-10-27 排查）：Bangumi API 的 ``rating.score`` 是**评分的原始均值**，
并不自带「样本够不够」的判断。因此一部只有 1 个人打分的冷门作品，如果这唯一的
一个人打了 10 分，API 就会如实返回 ``score = 10.0``：

    小狗亚斑  bangumi_id=210388  rating.score=10  rating.total=1  rank=0

而 Bangumi **站点前端**对这类低票条目是显示「--」的、``rank`` 也是 0（不进排行榜）。
我们过去只看 ``score > 0`` 就直接入库展示，于是作品库里凭空出现了一批「10 分作品」，
和源站观感不一致。

所以判断一个评分能不能展示，光看分数不够，**必须看评分人数**。本模块统一这个口径。

口径要点（重要）：**「人数未知」不等于「人数不足」**。加列之后存量的行都是 NULL，
如果 NULL 一律按不足处理，全站作品会瞬间变成「--」，那是事故。所以：

    rating_count 为 None  -> 未知 -> 按可信处理（维持原有展示，不改动存量观感）
    rating_count < 阈值   -> 样本不足 -> 视为无可参考评分，展示「--」
    rating_count >= 阈值  -> 可信 -> 照常展示分数

阈值 MIN_RATING_COUNT 是可以调的保守值，不是 Bangumi 官方口径的精确截取点
（官方并未公开这个数字）。观测到的事实是 ``total=1`` 时确实不显分不入榜。
"""

from __future__ import annotations

# 评分人数达到这个数量，才认为平均分有参考意义。
# 2026-10-28 用户定档 50：Bangumi 上只有两三个人打分的作品，均分能轻易到 9.5，
# 这种「高分」没有参考价值（实测 冒险兄弟/米菲/Незнайка 的 9.5 都只有 2 人打分）。
MIN_RATING_COUNT = 50


def is_credible(count: object) -> bool:
    """评分是否可信。count 为 None / 无法解析时一律返回 True（未知不算异常）。

    刻意这样兜底：只有**明确查到**人数且人数偏低时才判定不可信，避免因为
    数据来源不同（B站导入、投稿、老行没回填）造成大面积误判。
    """
    if count is None:
        return True
    try:
        return int(count) >= MIN_RATING_COUNT  # type: ignore[arg-type]
    except Exception:
        # 这里必须宽进：模板里喂进来的不一定是 ORM 对象。dict 缺 rating_count 键时
        # Jinja 返回 Undefined，对它 int() 抛的是 UndefinedError —— 既不是 TypeError
        # 也不是 ValueError，只按「未知」处理才不会让整个页面渲染失败
        # （2026-10-27 实测：persona / 作品库的 item 正是这种 dict，直接 500）。
        # 未知 -> 可信 -> 维持原有展示，这正是我们要的兜底方向。
        return True


def rating_shown(rating: object, count: object) -> float | None:
    """返回应当展示的分数；没有参考价值时返回 None（调用方渲染「--」）。

    - rating 无效（空 / <=0）：返回 None —— 这是原本就「没有评分」的情况，
      调用方的既有行为是整块不渲染，不要和「低票」混淆；
    - rating 有效但人数不足：返回 None —— 调用方据此渲染「--」；
    - 其余：返回 float(rating)。
    """
    if rating is None:
        return None
    try:
        value = float(rating)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if not is_credible(count):
        return None
    return value


def rating_display(rating: object, count: object, dash: str = "--") -> str:
    """返回可直接渲染的展示文本，供模板使用。

    三种返回，区分清楚才能既修掉「10 分」又不误伤别的情形：

    - ``"7.8"``  分数可用（人数够，或人数未知维持原样）；
    - ``"--"``   有分数但人数太少，没有参考价值 -> 显式显示「--」，对齐源站观感；
    - ``""``     本来就没有评分 -> 返回空串，调用方沿用既有的「整块不渲染」行为，
                 不要变成「--」，否则会把「无评分」和「低票」两种状态混为一谈。
    """
    value = rating_shown(rating, count)
    if value is not None:
        return f"{value:.1f}"
    try:
        raw = float(rating)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return dash if raw > 0 else ""


def effective_rating(rating: object, count: object) -> float:
    """排序 / 打分用的「有效评分」：人数不足、不足采信时按 0 参与计算。

    和展示用的 rating_display 是**两件事**，不要混用：

    - 展示要保留源站原值，连人数一起判断才渲染「--」；
    - 但排序和推荐加权必须让「1 个人打的 10 分」失去加成，否则这类作品会
      既显示在「评分最高」最前面，又被当成高分在首页推荐里加权顶上来
      （2026-10-28 实测就是这两个现象）。

    人数未知（None）维持原值：存量还没回填的行不该被误伤成 0。
    """
    value = rating_shown(rating, count)
    return float(value) if value is not None else 0.0
