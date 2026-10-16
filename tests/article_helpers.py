"""ANIPLEX NEWS BOX 假站点：给资讯导入测试提供最小可解析的 HTML。

真实站点的结构由 scripts/import_articles 的解析器负责，这里只按**同样的结构**拼装片段，
让测试既能覆盖「解析 -> 去重 -> 写入」的全流程，又完全不访问真实网络。
片段结构对照真实站点（2026-10）：

- 列表项：`<article class="news_archive__item c-article">` + `detail.html?id=` + `<date>` + `<h3 ...__ttl>`；
- 翻页按钮：`<button class="c-viewmore__btn" data-nextpage="2" data-query="/rest/news_list.html?...">`；
- 详情页：`h2.single__ttl` / `time.single__date` / `div.single__content` / `meta[property=og:description]`。
"""

from __future__ import annotations

from scripts.import_articles import ArticleFetchError, SITE_BASE


def list_item(
    detail_id: int,
    date_text: str,
    title: str,
    *,
    ip: str = "",
    archive: str = "news_archive__item",
) -> str:
    """拼一个带站内详情页的列表项。"""
    prefix = f"{ip}／\n                " if ip else ""
    return f"""<article class="{archive} c-article">
        <a href="detail.html?id={detail_id}">
            <div class="c-article__thumb -news">
                <img src="SYS/CONTENTS/keyvisual_{detail_id}" alt="" loading="lazy" />
                <date class="c-article__thumbdate">{date_text}</date>
            </div>
            <h3 class="{archive}__ttl c-article__ttl">
                {prefix}{title}
            </h3>
        </a>
    </article>"""


def external_item(
    url: str,
    date_text: str,
    title: str,
    *,
    origin: str = "",
    archive: str = "event_archive__item",
) -> str:
    """拼一个没有站内详情页、只有官网原文链接的列表项（活动栏目）。"""
    origin_block = (
        f'<h2 class="{archive}__origin">{origin}</h2>' if origin else ""
    )
    return f"""<article class="{archive} c-colsec__article c-article">
    <div class="{archive}__row">
      <div class="{archive}__info">
        <date class="{archive}__date c-article__date">{date_text}</date>
        {origin_block}
        <h3 class="{archive}__ttl c-article__ttl">{title}</h3>
      </div>
      <button class="{archive}__checkbtn">
        <a href="{url}" target="_blank" rel="noopener noreferrer">Check now</a>
      </button>
    </div>
  </article>"""


def list_page(items: list[str], *, next_query: str = "") -> str:
    """把若干列表项拼成一个栏目页（可选带「view more」翻页按钮）。"""
    more = ""
    if next_query:
        more = (
            '<div class="c-viewmore" data-pager="1">'
            f'<button class="c-viewmore__btn" data-nextpage="2" data-query="{next_query}">'
            '<i class="c-viewmore__btn__arw"></i></button></div>'
        )
    return (
        '<!DOCTYPE html><html><body><main class="l-main">'
        f'<section class="news_archive">{"".join(items)}</section>'
        f"{more}</main></body></html>"
    )


def detail_page(
    title: str,
    date_text: str,
    body_html: str,
    *,
    description: str = "",
    origin: str = "",
) -> str:
    """拼一个详情页；description 为空时站点会回落成标题（与真实站点一致）。"""
    desc = description or title
    origin_block = f'<h4 class="single__ip__ttl">{origin}</h4>' if origin else ""
    return f"""<!DOCTYPE html><html><head>
<meta property="og:description" content="{desc}">
</head><body>
<article class="single">
  {origin_block}
  <h2 class="single__ttl">
    {title}
  </h2>
  <time class="single__date c-article__date">{date_text}</time>
  <div class="single__content">
    {body_html}
  </div>
  <button class="single__back"><a href="/">Back to top</a></button>
</article>
</body></html>"""


def make_site(pages: dict[str, str], details: dict[int, str] | None = None):
    """构造 fetch(url) -> html：栏目/分页页按 URL 命中，详情页按 id 命中。"""
    details = details or {}

    def fetch(url: str) -> str:
        if url in pages:
            return pages[url]
        for detail_id, html in details.items():
            if url.endswith(f"detail.html?id={detail_id}"):
                return html
        raise ArticleFetchError(f"假站点没有这个地址：{url}")

    return fetch


def forbid_network(*_args, **_kwargs):
    """默认的 fetch_page 替身：任何真实网络访问都会让测试失败。"""
    raise AssertionError("测试不允许访问真实网络")


__all__ = [
    "SITE_BASE",
    "detail_page",
    "external_item",
    "forbid_network",
    "list_item",
    "list_page",
    "make_site",
]
