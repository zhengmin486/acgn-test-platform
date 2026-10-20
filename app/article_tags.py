"""资讯自动识别：从资讯文本识别标签并关联作品。

识别策略（受控词表匹配，只用标准库，不引入任何第三方依赖）：
- 标签词表 = 库中已有作品标签（tags 表，Bangumi 导入时按标注数入库）：
  它们是本站真实的题材词汇，比自由文本切词更稳，也不会造出筛选栏里
  点不出结果的新标签；
- 作品信息词表 = 库内作品的结构化信息（crud.list_work_info_terms）：
  作者 / 公司（entities，infobox 识别）、系列（series）、译名
  （title_original / aliases）。与题材标签是两套东西：题材回答
  「这篇资讯聊什么类型」，作品信息回答「这篇资讯在讲谁 / 哪部」，
  后者同时是作品关联的增强信号；
- 作品词表 = 库中作品标题（contents.title）与作品信息词表：标题 /
  译名命中即「这篇资讯在讲哪部作品」的答案，作者 / 公司 / 系列命中
  作补充关联；与作品详情页「相关资讯」（crud.list_related_articles，
  识别关联 ∪ 标题 LIKE）互为反向；
- 匹配方式 = 子串包含（不区分大小写，NFKC 归一全角/半角后比较）；
  纯 ASCII 短词（OFF / AGE）要求词边界，匹配前剔除 URL 与长 base64 串
  （否则 abema.tv/now-on-air 会命中作品《AIR》）；
- 打分 = 出现次数 x 字段权重（标题 3 / 摘要 2 / 正文 1），列表页
  「作品名／」前缀（ip_name）作为站点自身的分类信号额外加权
  （EXTRA_NAME_WEIGHT）；作品关联另有最低分 MIN_LINK_SCORE，正文里
  偶然出现 1 次的常用词作品名（「地下2階」）不关联；
- 作品信息加成 = 译名命中按标题同权重计（它就是作品自身的名字）；
  作者 / 公司 / 系列名命中按更低权重计（标题 2 / 摘要 1 / 正文 1，
  见 WORK_INFO_*），且只在没有标题 / 译名直接命中的作品里补关联
  （二级补充：标题精确命中的作品永远排在前列，实体命中不挤占名额）；
- 截断 = 得分从高到低取前 MAX_ARTICLE_TAGS 个题材标签、前
  MAX_ARTICLE_LINKS 部作品、前 MAX_ARTICLE_ENTITIES 个作品信息标签；
  词表长度不足 MIN_MATCH_LENGTH 的词不参与匹配（单字词在中文里
  噪声太大）。

写入语义（替换式，与作品标签一致）：
- crud.replace_article_tags / replace_article_content_links /
  replace_article_entities 先删旧关联再建新关联，重复识别幂等、
  不堆叠行；
- 识别只认「文本里真实出现」的词，不做任何推测性关联。

入口：
- 资讯导入（scripts/import_articles.py，apply 模式）自动对新文章执行；
- 存量回填用 scripts/link_articles.py（预览 / --apply / --force）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from app import crud
from app.models import Article, Content

# 单篇资讯最多保留的识别标签数（与 crud.MAX_ARTICLE_TAGS 一致，识别侧截断用）。
MAX_ARTICLE_TAGS = crud.MAX_ARTICLE_TAGS

# 单篇资讯最多关联的作品数（与作品详情页「相关内容」条数一致）。
MAX_ARTICLE_LINKS = 6

# 词表最小匹配长度：不足 2 个字符的词不进匹配（单字标签/标题噪声太大）。
MIN_MATCH_LENGTH = 2

# 字段权重：标题命中比摘要/正文更能说明主题。
TITLE_WEIGHT = 3
SUMMARY_WEIGHT = 2
BODY_WEIGHT = 1

# 「作品名／」前缀（ip_name）的加分：站点自身的分类信号，比一次标题提及
# 更强，但压不过正文里反复提及（EXTRA 加分 vs 多次 x 权重累加）。
EXTRA_NAME_WEIGHT = 6

# 纯 ASCII 字母数字词（OFF / AGE / PS / BEM）的匹配边界：要求前后不是
# 字母数字，避免「graniph_official」命中作品《OFF》、「message」命中《AGE》。
_ASCII_WORD = re.compile(r"[0-9a-z]+")

# 文本清洗：URL 与长 base64 串会带来大量假命中。
# 线上实例：abema.tv/now-on-air 里的 air 命中作品《AIR》；某篇正文混进的
# 图片 base64 命中作品《I/O》。匹配前先把这两类内容剔除。
_URL_RE = re.compile(r"https?://\S+")
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# 作品关联的最低得分：正文命中 1 次（1 分）不足以关联——OFF / 地下 / 人生
# 这类作品名也是正文里的普通词（「振動OFF」「地下2階」），单次命中必是噪声；
# 标题/摘要命中（3 / 2 分）或正文多次提及才可信；ip_name 加分（+6）不受影响。
MIN_LINK_SCORE = 2

# 作品信息（作者 / 公司 / 系列）命中权重：低于标题 / 译名命中（保守），
# 标题 2 / 摘要 1 / 正文 1。单个摘要或正文提及（1 分）不足以关联
# （低于 MIN_LINK_SCORE），标题提及或正文反复提及才补关联。
WORK_INFO_TITLE_WEIGHT = 2
WORK_INFO_SUMMARY_WEIGHT = 1
WORK_INFO_BODY_WEIGHT = 1

# 单篇资讯最多保留的作品信息标签数（与 crud.MAX_ARTICLE_ENTITIES 一致，
# 识别侧截断用）。
MAX_ARTICLE_ENTITIES = crud.MAX_ARTICLE_ENTITIES


@lru_cache(maxsize=32768)
def _ascii_pattern(needle: str) -> re.Pattern[str]:
    """纯 ASCII 词的词边界正则（needle 已归一为小写）。"""
    return re.compile(r"(?<![0-9a-z])" + re.escape(needle) + r"(?![0-9a-z])")


@lru_cache(maxsize=32768)
def _match_text(text: str) -> str:
    """匹配用文本：剔除 URL 与长 base64 串后再归一（结果缓存）。"""
    return _fold(_BASE64_RE.sub(" ", _URL_RE.sub(" ", text or "")))


@dataclass(frozen=True)
class WorkMatch:
    """一篇资讯关联到的一部作品（识别结果的最小单元）。

    tier：1 = 标题 / 译名直接命中（作品自身的名字出现在文本里）；
    2 = 作者 / 公司 / 系列补充命中（作品信息反查，权重更低）。
    排名时一级全部排在二级之前（保守：实体命中不挤占直接命中名额）。
    """

    content_id: int
    title: str
    score: int
    tier: int


@dataclass(frozen=True)
class WorkInfoTerm:
    """作品信息词表项：一个名字及其指向的作品。

    kind：entity=作者/公司 | series=系列 | alias=译名；
    entity_type：entity 专用的 person / company / association 快照；
    targets：(content_id, title, rating) 元组——实体 / 系列通常指向
    多部作品，译名只指向一部。
    """

    name: str
    kind: str
    entity_type: str | None = None
    targets: tuple[tuple[int, str, float], ...] = ()


@dataclass(frozen=True)
class MatchedWorkInfo:
    """一篇资讯命中的一个作品信息标签（展示 + 落库用）。

    content_id：译名行的目标作品（展示链接 /contents/{id} 用）；实体 /
    系列行为 None（链接走 /creators/{name}）。
    """

    name: str
    kind: str
    entity_type: str | None = None
    content_id: int | None = None
    score: int = 0


@dataclass(frozen=True)
class ArticleRecognition:
    """一篇资讯的识别结果（预览与写入共用同一结构）。

    tags：得分前 MAX_ARTICLE_TAGS 的题材标签名（已序，不再做卫生裁剪——
    裁剪由 crud.replace_article_tags 落库时兜底）；
    work_matches：得分前 MAX_ARTICLE_LINKS 的关联作品（标题 / 译名直接
    命中为一级，作者 / 公司 / 系列补充命中为二级，一级全部排在二级前）；
    work_info：得分前 MAX_ARTICLE_ENTITIES 的作品信息标签（作者 / 公司 /
    系列 / 译名，展示用；与 work_matches 独立截断）。
    content_titles：work_matches 的标题列表（兼容旧调用方）。
    """

    article_id: int
    tags: list[str] = field(default_factory=list)
    work_matches: list[WorkMatch] = field(default_factory=list)
    work_info: list[MatchedWorkInfo] = field(default_factory=list)

    @property
    def content_titles(self) -> list[str]:
        return [match.title for match in self.work_matches]


@lru_cache(maxsize=32768)
def _fold(text: str) -> str:
    """匹配前的文本归一：小写 + NFKC（全角转半角、兼容字符归一）。

    日文源站的标题常用全角符号（！／Ａ），作品库里的标题则混用半角，
    不归一的话「孤独摇滚！」与「孤独摇滚!」会互相漏掉。

    结果按文本缓存：识别是「一篇资讯 x 整个词表」的双重循环，每篇的
    title/summary/body 会被归一上万次，不缓存时全量回填要跑好几分钟。
    词表词只有一份字符串对象，跨文章全部命中缓存。
    """
    return unicodedata.normalize("NFKC", text or "").lower()


def _count_hits(needle: str, haystack: str) -> int:
    """统计词在已归一文本里的出现次数（needle / haystack 都已是 _fold 结果）。

    - 纯 ASCII 字母数字词走词边界：作品《OFF》不会因为正文里有
      「graniph_official」而命中，标签「AGE」也不会命中「message」；
    - 中文 / 日文等无空格文本仍按子串计数（「机甲」命中「机甲动画甲」）。
    """
    hits = haystack.count(needle)  # 先用子串快速预筛：绝大多数词不出现
    if hits == 0 or not _ASCII_WORD.fullmatch(needle):
        return hits
    return len(_ascii_pattern(needle).findall(haystack))


def score_term(term: str, title: str = "", summary: str = "", body: str = "") -> int:
    """一个词表词在三段文本里的得分：出现次数 x 字段权重（不区分大小写）。

    长度不足 MIN_MATCH_LENGTH 的词直接返回 0（不参与匹配）。
    """
    name = (term or "").strip()
    if len(name) < MIN_MATCH_LENGTH:
        return 0
    needle = _fold(name)
    total = 0
    for weight, text in (
        (TITLE_WEIGHT, title or ""),
        (SUMMARY_WEIGHT, summary or ""),
        (BODY_WEIGHT, body or ""),
    ):
        if text:
            total += _count_hits(needle, _match_text(text)) * weight
    return total


def _headline_score(term: str, title: str = "", summary: str = "") -> int:
    """词在标题/摘要里的得分（正文不计）。

    短 ASCII 作品名（OFF / AIR / BEM）靠标题或摘要证明主题归属，正文里
    的普通用法（振動OFF / now-on-air）不能当证据。
    """
    needle = _fold(term)
    total = 0
    for weight, text in ((TITLE_WEIGHT, title or ""), (SUMMARY_WEIGHT, summary or "")):
        if text:
            total += _count_hits(needle, _match_text(text)) * weight
    return total


def _rank(scored: list[tuple[int, str]], limit: int) -> list[str]:
    """按得分从高到低排序，同分按名称字典序（稳定，不随库内状态漂移）。"""
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [term for _, term in scored[:limit]]


def match_tags(
    title: str = "",
    summary: str = "",
    body: str = "",
    tag_names=(),
    limit: int = MAX_ARTICLE_TAGS,
) -> list[str]:
    """从标签词表里识别资讯涉及的标签（受控词表，得分前 limit 个）。"""
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in tag_names:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        score = score_term(name, title, summary, body)
        if score > 0:
            scored.append((score, name))
    return _rank(scored, limit)


def _prepare_extras(extra_names: Sequence[str]) -> list[str]:
    """ip_name 等强信号的归一形式（不足最小匹配长度的丢弃）。"""
    return [
        folded
        for folded in (_fold(raw) for raw in extra_names or ())
        if len(folded) >= MIN_MATCH_LENGTH
    ]


def _direct_term_score(
    name: str,
    title: str,
    summary: str,
    body: str,
    extras: list[str],
) -> tuple[int, bool] | None:
    """标题 / 译名词的得分（None = 不达标）。

    与作品标题匹配同一套规则：字段权重 3/2/1 + ip_name 加分（互为子串）+
    MIN_LINK_SCORE 门槛 + 纯 ASCII 短词的头条守卫。返回 (得分, 是否
    extra 命中)。

    最小匹配长度在加分之前检查：单字标题（心）不得靠 ip_name 子串加分
    绕过下限（线上真实误关联：ip_name「楽園追放 心のレゾナンス」把
    作品《心》挂进关联列表）。
    """
    if len(name) < MIN_MATCH_LENGTH:
        return None
    folded = _fold(name)
    score = score_term(name, title, summary, body)
    extra_hit = bool(extras) and any(
        extra in folded or folded in extra for extra in extras
    )
    if extra_hit:
        score += EXTRA_NAME_WEIGHT
    if score < MIN_LINK_SCORE:
        return None
    # 纯 ASCII 短词（OFF / AIR / BEM）只认标题/摘要命中或 ip_name 指向：
    # 正文里的「振動OFF」「now-on-air」这类普通用法出现再多次也不算数。
    if not extra_hit and _ASCII_WORD.fullmatch(folded) and len(folded) <= 3:
        if _headline_score(name, title, summary) == 0:
            return None
    return score, extra_hit


def _work_info_score(name: str, title: str, summary: str, body: str) -> int:
    """作者 / 公司 / 系列名的得分：字段权重 2/1/1（低于标题 / 译名）。

    纯 ASCII 短词（KEY / IG）同样受头条守卫：正文里的普通用法
    （"press key"）出现再多次也不算数。
    """
    if len(name) < MIN_MATCH_LENGTH:
        return 0
    needle = _fold(name)
    total = 0
    for weight, text in (
        (WORK_INFO_TITLE_WEIGHT, title or ""),
        (WORK_INFO_SUMMARY_WEIGHT, summary or ""),
        (WORK_INFO_BODY_WEIGHT, body or ""),
    ):
        if text:
            total += _count_hits(needle, _match_text(text)) * weight
    if total <= 0:
        return 0
    if _ASCII_WORD.fullmatch(needle) and len(needle) <= 3:
        if _headline_score(name, title, summary) == 0:
            return 0
    return total


def build_info_terms(rows) -> list[WorkInfoTerm]:
    """把 crud.list_work_info_terms 的行按 (name, kind) 分组成词表项。

    同一名字同一种类只保留一个词表项，targets 按 content_id 去重
    （一个实体可以多个角色关联同一作品）。
    """
    grouped: dict[tuple[str, str], dict] = {}
    for name, kind, entity_type, content_id, title, rating in rows:
        name = (name or "").strip()
        if not name:
            continue
        entry = grouped.setdefault((name, kind), {"entity_type": entity_type, "targets": {}})
        entry["targets"][content_id] = (content_id, title, rating)
    return [
        WorkInfoTerm(
            name=name,
            kind=kind,
            entity_type=entry["entity_type"],
            targets=tuple(entry["targets"].values()),
        )
        for (name, kind), entry in grouped.items()
    ]


def load_vocabulary(db) -> tuple[list[str], list[tuple[int, str]], list[WorkInfoTerm]]:
    """一次查全三类词表（导入批次 / 回填共用，避免每篇资讯重复查询）。

    返回 (题材标签名, 作品标题 (id, title), 作品信息词表)。
    """
    return (
        crud.list_all_tag_names(db),
        crud.list_content_titles(db),
        build_info_terms(crud.list_work_info_terms(db)),
    )


def match_article_works(
    title: str = "",
    summary: str = "",
    body: str = "",
    title_terms: Sequence[tuple[int, str]] = (),
    info_terms: Sequence[WorkInfoTerm] = (),
    *,
    extra_names: Sequence[str] = (),
    limit: int = MAX_ARTICLE_LINKS,
) -> list[WorkMatch]:
    """识别资讯关联的作品：标题 / 译名直接命中 + 作品信息补充命中。

    title_terms：作品标题词表 (content_id, title)（按 id 排序，同名
    只认第一条）；info_terms：作品信息词表（build_info_terms 产物）。

    排名规则（保守）：一级（标题 / 译名直接命中）全部排在二级（作者 /
    公司 / 系列补充命中）之前；一级内按得分降序、同分按标题名字序
    （与旧实现一致）；二级内按得分降序、同分按评分降序。二级只在
    一级未满 limit 时补位——实体命中永远不挤占直接命中的名额。
    """
    extras = _prepare_extras(extra_names)
    scores: dict[int, int] = {}
    titles: dict[int, str] = {}
    direct: set[int] = set()

    # 一级：作品标题（同名只认 id 最小的一条）
    seen_titles: set[str] = set()
    for content_id, name in title_terms:
        name = (name or "").strip()
        if not name or name in seen_titles:
            continue
        seen_titles.add(name)
        scored = _direct_term_score(name, title, summary, body, extras)
        if scored is None:
            continue
        scores[content_id] = scores.get(content_id, 0) + scored[0]
        titles[content_id] = name
        direct.add(content_id)

    # 一级：译名（作品自身的其他名字，与标题同权重）
    # 二级：作者 / 公司 / 系列（作品信息反查，低权重补充）
    info_scores: dict[int, int] = {}
    ratings: dict[int, float] = {}
    for term in info_terms:
        if term.kind == "alias":
            scored = _direct_term_score(term.name, title, summary, body, extras)
            if scored is None:
                continue
            for content_id, content_title, _rating in term.targets:
                scores[content_id] = scores.get(content_id, 0) + scored[0]
                titles.setdefault(content_id, content_title)
                direct.add(content_id)
            continue
        info_score = _work_info_score(term.name, title, summary, body)
        if info_score <= 0:
            continue
        for content_id, content_title, rating in term.targets:
            ratings[content_id] = rating
            titles.setdefault(content_id, content_title)
            if content_id in direct:
                # 直接命中的作品也吃作品信息分：多重信号提升其排名，
                # 但不改变它的一级身份。
                scores[content_id] += info_score
            else:
                info_scores[content_id] = info_scores.get(content_id, 0) + info_score

    tier1 = sorted(direct, key=lambda content_id: (-scores[content_id], titles[content_id]))
    tier2 = sorted(
        (cid for cid, score in info_scores.items() if score >= MIN_LINK_SCORE),
        key=lambda content_id: (
            -info_scores[content_id],
            -ratings.get(content_id, 0.0),
            content_id,
        ),
    )
    matches = [WorkMatch(cid, titles[cid], scores[cid], 1) for cid in tier1]
    for content_id in tier2:
        if len(matches) >= limit:
            break
        matches.append(WorkMatch(content_id, titles[content_id], info_scores[content_id], 2))
    # 同名作品只保留排名最高的一条：词表按标题去重过，但作品信息反查
    # （实体 / 系列的 targets）会带出同名重复条目（线上 Fate/Zero、
    # 孤独摇滚！就有），关联列表里出现两张同名卡对用户没有意义。
    deduped: list[WorkMatch] = []
    seen_display: set[str] = set()
    for match in matches:
        if match.title in seen_display:
            continue
        seen_display.add(match.title)
        deduped.append(match)
    return deduped[:limit]


def match_content_titles(
    title: str = "",
    summary: str = "",
    body: str = "",
    content_titles=(),
    *,
    extra_names=(),
    limit: int = MAX_ARTICLE_LINKS,
) -> list[str]:
    """从作品标题词表里识别资讯关联的作品（得分前 limit 个；仅标题匹配）。

    纯标题口径的兼容入口（作品信息增强走 match_article_works）：词表
    只有作品标题，没有译名 / 实体补充，语义与作品信息上线前一致。

    extra_names：站点列表页的「作品名／」前缀等强信号——与词表词互为
    子串（都够 MIN_MATCH_LENGTH）即加分，用于标题里没复述作品名的条目。
    """
    title_terms = [(index, name) for index, name in enumerate(content_titles)]
    return [
        match.title
        for match in match_article_works(
            title,
            summary,
            body,
            title_terms,
            (),
            extra_names=extra_names,
            limit=limit,
        )
    ]


def match_work_info(
    title: str = "",
    summary: str = "",
    body: str = "",
    info_terms: Sequence[WorkInfoTerm] = (),
    limit: int = MAX_ARTICLE_ENTITIES,
) -> list[MatchedWorkInfo]:
    """从作品信息词表里识别资讯涉及的作品信息标签（得分前 limit 个）。

    与关联打分（match_article_works）同一套权重，但不设 MIN_LINK_SCORE：
    标签是展示信号，正文提及一次的作者也值得展示；关联才需要更高置信。
    译名行的 content_id 取其指向的作品（展示链接 /contents/{id} 用）。
    """
    scored: list[MatchedWorkInfo] = []
    for term in info_terms:
        if term.kind == "alias":
            score = score_term(term.name, title, summary, body)
        else:
            score = _work_info_score(term.name, title, summary, body)
        if score <= 0:
            continue
        content_id = (
            term.targets[0][0] if term.kind == "alias" and term.targets else None
        )
        scored.append(
            MatchedWorkInfo(
                name=term.name,
                kind=term.kind,
                entity_type=term.entity_type,
                content_id=content_id,
                score=score,
            )
        )
    scored.sort(key=lambda item: (-item.score, item.name))
    return scored[:limit]


def recognize(
    article: Article,
    *,
    tag_names=(),
    title_terms=(),
    info_terms=(),
    extra_names=(),
) -> ArticleRecognition:
    """只识别、不写库（预览与 CLI dry-run 用）。

    title_terms：作品标题词表 (content_id, title)（crud.list_content_titles
    的产物）；info_terms：作品信息词表（build_info_terms 的产物）。
    """
    return ArticleRecognition(
        article_id=article.id,
        tags=match_tags(
            article.title, article.summary, article.body, tag_names
        ),
        work_matches=match_article_works(
            article.title,
            article.summary,
            article.body,
            title_terms,
            info_terms,
            extra_names=extra_names,
        ),
        work_info=match_work_info(
            article.title, article.summary, article.body, info_terms
        ),
    )


def apply_recognition(db, recognition: ArticleRecognition) -> None:
    """把识别结果写库（替换式）；不 commit——由调用方按事务边界统一提交。

    关联作品按 content_id 取库内对象：识别与写入之间作品可能已被删
    （弱关联语义：取不到即跳过，不写悬空连接行）。

    结尾 flush：会话常以 autoflush=False 创建（app.database 即如此），
    不 flush 的话同一会话内随后到来的查询看不到刚建的关联行。
    """
    crud.replace_article_tags(db, recognition.article_id, recognition.tags)
    works = []
    for match in recognition.work_matches:
        content = db.get(Content, match.content_id)
        if content is not None:
            works.append(content)
    crud.replace_article_content_links(db, recognition.article_id, works)
    crud.replace_article_entities(
        db,
        recognition.article_id,
        [
            (item.name, item.kind, item.entity_type, item.content_id, item.score)
            for item in recognition.work_info
        ],
    )
    db.flush()


def link_article(
    db,
    article: Article,
    *,
    tag_names=(),
    title_terms=(),
    info_terms=(),
    extra_names=(),
) -> ArticleRecognition:
    """识别并写入单篇资讯（导入批次用）；返回识别结果。"""
    recognition = recognize(
        article,
        tag_names=tag_names,
        title_terms=title_terms,
        info_terms=info_terms,
        extra_names=extra_names,
    )
    apply_recognition(db, recognition)
    return recognition


def link_articles(db, articles, *, extra_names_map=None) -> dict[int, ArticleRecognition]:
    """批量识别一批资讯：词表只查一次，逐篇识别写入（不 commit）。

    extra_names_map：{article_id: [额外作品名...]}（导入时传列表页的
    ip_name）。articles 需已 flushed（有 id），否则关联行引用不到文章。
    """
    if not articles:
        return {}
    tag_names, title_terms, info_terms = load_vocabulary(db)
    extra_names_map = extra_names_map or {}
    results: dict[int, ArticleRecognition] = {}
    for article in articles:
        results[article.id] = link_article(
            db,
            article,
            tag_names=tag_names,
            title_terms=title_terms,
            info_terms=info_terms,
            extra_names=extra_names_map.get(article.id, ()),
        )
    return results
