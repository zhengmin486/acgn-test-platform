"""资讯日文标题/摘要的自动翻译（OpenAI 兼容接口）。

设计取舍：
- 配置优先读 ``NEWS_LLM_*``，未配则回退 ``PERSONA_LLM_*``（同一套 provider 预设：
  deepseek / doubao / glm，也可指向任意兼容服务）；
- 两边都没配 Key 时 ``translate_available()`` 为 False，采集管线据此**跳过日文源**，
  只采中文源——宁可少而可读，也不往站内灌看不懂的日文条目；
- 翻译失败不阻断入库：调用方拿到 None 就退回原文（并由 source 名标注来源）；
- 只翻标题与摘要（RSS 正文本来就是摘要长度），不做逐段长文翻译，成本可控。
"""

from __future__ import annotations

import re

from app.config import settings
from app.persona import PersonaError, call_llm, resolve_llm_config

# 一批最多翻多少条：一条 prompt 里塞太多行容易漏行，20 条以内稳定。
BATCH_SIZE = 10

PROMPT_TEMPLATE = """你是动漫游戏资讯站的编辑。把下面每一条日文标题/摘要翻译成简体中文。

要求：
1. 严格按原顺序输出，一条一行，行首保留原来的编号与点号（如「1. 」）；
2. 只输出译文，不要解释、不要加引号、不要合并或拆分条目；
3. 作品名、人名、公司名用中文圈的通行译名；没有通行译名就保留日文原名；
4. 标题不要加感叹号或书名号以外的新标点。

待翻译：
{lines}"""

_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*[.、]\s*")


def translate_config() -> dict:
    """合并 NEWS_LLM_* 与 PERSONA_LLM_* 的翻译配置（含 ready / hint）。"""
    return resolve_llm_config(
        provider=settings.news_llm_provider or settings.persona_llm_provider,
        base_url=settings.news_llm_base_url or settings.persona_llm_base_url,
        api_key=settings.news_llm_api_key or settings.persona_llm_api_key,
        model=settings.news_llm_model or settings.persona_llm_model,
    )


def translate_available(cfg: dict | None = None) -> bool:
    """是否具备自动翻译能力（有可用的 Key + 地址 + 模型）。"""
    config = cfg if cfg is not None else translate_config()
    return bool(config.get("ready"))


def _strip_number(line: str) -> str:
    return _NUMBERED_LINE_RE.sub("", line or "").strip()


def translate_texts(
    texts: list[str],
    *,
    cfg: dict | None = None,
    call=None,
    timeout: float | None = None,
) -> list[str] | None:
    """批量把日文文本翻成中文；返回与输入同长度的列表，失败返回 None。

    call：可注入的 ``call(prompt) -> str``，测试用它替代真实网络。
    行数与输入不一致时视为失败（宁可回退原文，也不错位串行）。
    """
    items = [text or "" for text in texts]
    if not items:
        return []
    config = cfg if cfg is not None else translate_config()

    def _call(prompt: str) -> str:
        if call is not None:
            return call(prompt)
        return call_llm(
            prompt,
            base_url=config["base_url"],
            api_key=config["api_key"],
            model=config["model"],
            timeout=timeout or settings.news_translate_timeout,
            max_tokens=1500,
            temperature=0.2,
        )

    result: list[str] = []
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        numbered = [f"{index}. {text}" for index, text in enumerate(batch, start=1)]
        prompt = PROMPT_TEMPLATE.format(lines="\n".join(numbered))
        try:
            raw = _call(prompt)
        except (PersonaError, KeyError, OSError, TimeoutError):
            return None
        lines = [line for line in (raw or "").splitlines() if line.strip()]
        if len(lines) != len(batch):
            return None
        result.extend(_strip_number(line) or original for line, original in zip(lines, batch))
    return result
