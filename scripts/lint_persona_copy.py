"""人格文案体检：按 .workbuddy/memory/persona_copy_guide.md 的标准扫一遍全部称号。

用法：
    .venv/Scripts/python.exe scripts/lint_persona_copy.py
    .venv/Scripts/python.exe scripts/lint_persona_copy.py --strict   # 警告也算失败

返回码：0 = 无硬性问题；1 = 有。

为什么要这个脚本：称号 / 毒舌是「几十支、几百条」的量，靠人眼过一遍既不
可靠也不可重复。把「能不能截图转发」拆成可检查的几条（必须有 3 变体、
长度上限、禁用语、句式不能同构、量化锚占比），改文案时跑一次就知道有没有退步。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import persona  # noqa: E402

# 全网通用梗：圈外人也能接，等于没有圈内共鸣
BANNED = ("现实生活", "母单", "现充爆炸", "被生活")
# 量化锚：数字 / 次数 / 时长 / 倍数 / 全量词，至少要有一个
QUANT = re.compile(r"[0-9]|一年|两年|三年|一集|一部|一半|一场|一个字|全|零|百|千|倍")
MAX_LEN = 45
MIN_VARIANTS = 3
MIN_QUANT_SHARE = 0.25


def variants(note) -> list[str]:
    return list(note) if isinstance(note, (list, tuple)) else [note]


def collect() -> list[tuple[str, str, list[str]]]:
    """返回 [(来源, 称号, [变体文案...]), ...]。"""
    rows: list[tuple[str, str, list[str]]] = []
    for name, pool in (
        ("COMBO", persona.TITLE_COMBO),
        ("EXTREME", persona.TITLE_EXTREME),
        ("FANDOM", persona.TITLE_FANDOM),
    ):
        for item in pool:
            rows.append((name, item[1], variants(item[-1])))
    for cluster, row in persona.TITLE_MAIN.items():
        for _feat, tup in row.items():
            rows.append((f"MAIN/{cluster}", tup[0], variants(tup[-1])))
    for _ident, tup in persona.TITLE_IDENTITY.items():
        rows.append(("IDENTITY", tup[0], variants(tup[-1])))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="把警告也当失败")
    args = parser.parse_args()

    rows = collect()
    sentences = [s for _src, _title, vs in rows for s in vs]
    errors: list[str] = []
    warnings: list[str] = []

    for src, title, vs in rows:
        if len(vs) < MIN_VARIANTS:
            errors.append(
                f"[单句] {src} / {title}：只有 {len(vs)} 条（要求 ≥{MIN_VARIANTS}）"
            )
        for s in vs:
            if len(s) > MAX_LEN:
                errors.append(f"[超长 {len(s)} 字] {src} / {title}：{s}")
            for bad in BANNED:
                if bad in s:
                    errors.append(f"[禁用语「{bad}」] {src} / {title}：{s}")
        heads = Counter(s[:3] for s in vs)
        for head, count in heads.items():
            if count > 1:
                errors.append(f"[句式同构「{head}…」x{count}] {src} / {title}")

    for sentence, count in Counter(sentences).items():
        if count > 1:
            warnings.append(f"[重复句 x{count}] {sentence}")

    quant = [s for s in sentences if QUANT.search(s)]
    share = len(quant) / len(sentences) if sentences else 0.0
    if share < MIN_QUANT_SHARE:
        warnings.append(
            f"[量化不足] 带数字/量词的只有 {share:.0%}（目标 ≥{MIN_QUANT_SHARE:.0%}）"
        )

    avg_len = sum(map(len, sentences)) / len(sentences) if sentences else 0
    print(f"称号 {len(rows)} 支 / 文案 {len(sentences)} 条")
    print(f"量化锚 {share:.0%} · 平均 {avg_len:.1f} 字 · 最长 {max(map(len, sentences), default=0)} 字")
    print()
    for line in errors:
        print("ERROR ", line)
    for line in warnings:
        print("WARN  ", line)
    if not errors and not warnings:
        print("全部通过。")

    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
