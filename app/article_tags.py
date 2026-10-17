"""资讯自动识别：从资讯文本识别标签并关联作品。

识别策略（受控词表匹配，只用标准库，不引入任何第三方依赖）：
- 标签词表 = 库中已有作品标签（tags 表，Bangumi 导入时按标注数入库）：
  它们是本站真实的题材词汇，比自由文本切词更稳，也不会造出筛选栏里
  点不出结果的新标签；
- 作品词表 = 库中作品标题（contents.title）：标题命中即「这篇资讯在讲
  哪部作品」的答案，与作品详情页「相关资讯」（crud.list_related_articles，
  请求时实时匹配）互为反向；
- 匹配方式 = 子串包含（不区分大小写，NFKC 归一全角/半角后比较）；
  打分 = 出现次数 x 字段权重（标题 3 / 摘要 2 / 正文 1），列表页
  「作品名／」前缀（ip_name）作为站点自身的分类信号额外加权
  （EXTRA_NAME_WEIGHT）；
- 截断 = 得分从高到低取前 MAX_ARTICLE_TAGS 个标签、前 MAX_ARTICLE_LINKS
  部作品；词表长度不足 MIN_MATCH_LENGTH 的词不参与匹配（单字词在中文里
  噪声太大）。

写入语义（替换式，与作品标签一致）：
- crud.replace_article_tags / crud.replace_article_content_links 先删旧
  关联再建新关联，重复识别幂等、不堆叠行；
- 识别只认「文本里真实出现」的词，不做任何推测性关联。

入口：
- 资讯导入（scripts/import_articles.py，apply 模式）自动对新文章执行；
- 存量回填用 scripts/link_articles.py（预览 / --apply / --force）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from app import crud
from app.models import Article

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


@dataclass(frozen=True)
class ArticleRecognition:
    """一篇资讯的识别结果（预览与写入共用同一结构）。

    tags：得分前 MAX_ARTICLE_TAGS 的标签名（已序，不再做卫生裁剪——
    裁剪由 crud.replace_article_tags 落库时兜底）；
    content_titles：得分前 MAX_ARTICLE_LINKS 的作品标题（已序）。
    """

    article_id: int
    tags: list[str] = field(default_factory=list)
    content_titles: list[str] = field(default_factory=list)


def _fold(text: str) -> str:
    """匹配前的文本归一：小写 + NFKC（全角转半角、兼容字符归一）。

    日文源站的标题常用全角符号（！／Ａ），作品库里的标题则混用半角，
    不归一的话「孤独摇滚！」与「孤独摇滚!」会互相漏掉。
    """
    return unicodedata.normalize("NFKC", text or "").lower()


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
            total += _fold(text).count(needle) * weight
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


def match_content_titles(
    title: str = "",
    summary: str = "",
    body: str = "",
    content_titles=(),
    *,
    extra_names=(),
    limit: int = MAX_ARTICLE_LINKS,
) -> list[str]:
    """从作品标题词表里识别资讯关联的作品（得分前 limit 个）。

    extra_names：站点列表页的「作品名／」前缀等强信号——与词表词互为
    子串（都够 MIN_MATCH_LENGTH）即加分，用于标题里没复述作品名的条目。
    """
    extras = [_fold(name) for name in extra_names]
    extras = [name for name in extras if len(name) >= MIN_MATCH_LENGTH]

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in content_titles:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        score = score_term(name, title, summary, body)
        if extras:
            folded = _fold(name)
            if any(extra in folded or folded in extra for extra in extras):
                score += EXTRA_NAME_WEIGHT
        if score > 0:
            scored.append((score, name))
    return _rank(scored, limit)


def recognize(
    article: Article,
    *,
    tag_names=(),
    content_titles=(),
    extra_names=(),
) -> ArticleRecognition:
    """只识别、不写库（预览与 CLI dry-run 用）。"""
    return ArticleRecognition(
        article_id=article.id,
        tags=match_tags(
            article.title, article.summary, article.body, tag_names
        ),
        content_titles=match_content_titles(
            article.title,
            article.summary,
            article.body,
            content_titles,
            extra_names=extra_names,
        ),
    )


def apply_recognition(db, recognition: ArticleRecognition) -> None:
    """把识别结果写库（替换式）；不 commit——由调用方按事务边界统一提交。

    结尾 flush：会话常以 autoflush=False 创建（app.database 即如此），
    不 flush 的话同一会话内随后到来的查询看不到刚建的关联行。
    """
    crud.replace_article_tags(db, recognition.article_id, recognition.tags)
    crud.replace_article_content_links(db, recognition.article_id, recognition.content_titles)
    db.flush()


def link_article(
    db,
    article: Article,
    *,
    tag_names=(),
    content_titles=(),
    extra_names=(),
) -> ArticleRecognition:
    """识别并写入单篇资讯（导入批次用）；返回识别结果。"""
    recognition = recognize(
        article,
        tag_names=tag_names,
        content_titles=content_titles,
        extra_names=extra_names,
    )
    apply_recognition(db, recognition)
    return recognition


def link_articles(db, articles, *, extra_names_map=None) -> dict[int, ArticleRecognition]:
    """批量识别一批资讯：词表只查一次，逐篇识别写入（不 commit）。

    extra_names_map：{article_id: [额外作品名...]}（导入时传列表页的
    ip_name）。articles 需已 flush（有 id），否则关联行引用不到文章。
    """
    if not articles:
        return {}
    tag_names = crud.list_all_tag_names(db)
    content_titles = [title for _, title in crud.list_content_titles(db)]
    extra_names_map = extra_names_map or {}
    results: dict[int, ArticleRecognition] = {}
    for article in articles:
        results[article.id] = link_article(
            db,
            article,
            tag_names=tag_names,
            content_titles=content_titles,
            extra_names=extra_names_map.get(article.id, ()),
        )
    return results
