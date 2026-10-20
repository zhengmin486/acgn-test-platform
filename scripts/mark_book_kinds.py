"""给小说条目补书籍细分标注（contents.book_kind），并按同样口径清理非日本语种小说。

背景（2026-10-19）：Bangumi 书籍区只有一个 platform「小说」，里面既有文库出身的
轻小说，也有日本一般文艺，还有中译本 / 中国古典 / 欧美名著。早期导入把它们一律
映射成 novel，站内又把 novel 标成「轻小说」，结果轻小说区混进《罪与罚》《红楼梦》。

站的口径（用户 2026-10-19 定）：

    - 轻小说 light_novel｜日本普通书 literature —— 两者类型都是 novel，
      literature 只是**标注**（一枚「文学」角标），不是一个独立类型；
    - 非日本语种的小说：本站不收，从作品库删除（先备份数据库再跑 --apply）。

判定来源两条路，按顺序取第一个可用的：

1. ``--map <file.json>``：手工策展的判定表，形如
   ``{"2238": "light_novel", "1234": "literature", "4321": "drop"}``。
   **服务器不通 Bangumi 时只能走这条路**（线上库就是这样）。
2. 直接拉 Bangumi 条目 JSON，交给 ``scripts/import_bangumi.resolve_book`` 判定。
   需要出网，本机跑的话通常还要配代理（见 --proxy）。

用法：
    .venv/bin/python scripts/mark_book_kinds.py --map decisions.json        # 预览
    .venv/bin/python scripts/mark_book_kinds.py --map decisions.json --apply
    .venv/bin/python scripts/mark_book_kinds.py --proxy http://127.0.0.1:9910  # 在线预览
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app import crud  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.models import Content  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    BOOK_KIND_LIGHT_NOVEL,
    BOOK_KIND_LITERATURE,
    build_opener,
    fetch_subject,
    resolve_book,
)

DECISION_SET = "set"  # 已写入标注
DECISION_DROP = "drop"  # 非日本语种 -> 删除
DECISION_UNDECIDED = "undecided"  # 既没策展条目也拉不到数据

DROP_REASON = "非日本语种小说：本站不收录"


def load_map(path: str) -> tuple[dict[int, str], dict[int, str]]:
    """读策展判定表，返回 ``(按 bangumi_id 索引, 按 content_id 索引)``。

    键有两种写法：
    - ``"2238": ...``      —— bangumi 条目 ID（自动入库的条目都有）；
    - ``"cid:9407": ...``  —— 站内作品 ID，用于词条申请进来的、**没有** bangumi
      来源的手工条目（这类条目拉不到 Bangumi 数据，只能手工策展）。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    by_bangumi: dict[int, str] = {}
    by_content: dict[int, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or value not in ("light_novel", "literature", "drop"):
            raise SystemExit(f"判定值非法：{key} -> {value!r}（应为 light_novel / literature / drop）")
        target = by_content if key.startswith("cid:") else by_bangumi
        raw_key = key[4:] if key.startswith("cid:") else key
        try:
            target[int(raw_key)] = value
        except ValueError:
            raise SystemExit(f"判定表的键必须是数字或 cid:<数字>：{key!r}")
    return by_bangumi, by_content


def decide_for(row: Content, decisions, fetcher) -> tuple[str, str | None]:
    """返回 (decision, book_kind)。book_kind 只在 decision=set 时有意义。"""
    by_bangumi, by_content = decisions
    curated = by_content.get(row.id)
    if curated is None and row.bangumi_id is not None:
        curated = by_bangumi.get(row.bangumi_id)
    if curated == "drop":
        return DECISION_DROP, None
    if curated in (BOOK_KIND_LIGHT_NOVEL, BOOK_KIND_LITERATURE):
        return DECISION_SET, curated
    if row.bangumi_id is None:
        # 没有 Bangumi 来源又没写进判定表：只能人工看一眼再补。
        return DECISION_UNDECIDED, None
    if fetcher is None:
        return DECISION_UNDECIDED, None
    try:
        payload = fetcher(row.bangumi_id)
    except Exception:  # 网络类异常：单条失败不该中断整批
        return DECISION_UNDECIDED, None
    content_type, book_kind = resolve_book(payload)
    if content_type is None:
        return DECISION_DROP, None
    if book_kind is None:
        return DECISION_SET, BOOK_KIND_LIGHT_NOVEL if row.type == "novel" else None
    return DECISION_SET, book_kind


def main() -> int:
    parser = argparse.ArgumentParser(description="回填书籍细分标注 / 清理非日本语种小说")
    parser.add_argument("--map", help="策展判定表 JSON：{bangumi_id: light_novel|literature|drop}")
    parser.add_argument("--apply", action="store_true", help="真正写库；默认是预览")
    parser.add_argument("--proxy", help="在线判定时使用的代理（本机常用 http://127.0.0.1:9910）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单条目请求超时（秒）")
    args = parser.parse_args()

    decisions: tuple[dict[int, str], dict[int, str]] = ({}, {})
    if args.map:
        decisions = load_map(args.map)

    fetcher = None
    if not args.map:
        opener = build_opener(args.proxy)

        def fetcher(subject_id: int) -> dict:  # type: ignore[misc]
            return fetch_subject(subject_id, opener=opener, timeout=args.timeout, retries=1)

    # engine / SessionLocal 走 app.database 的默认库连接（ DATABASE_URL ）：
    # 与线上服务同一个库，本脚本只做补标与清理，不在库之间搬运数据。
    crud.init_db(engine)
    db = SessionLocal()

    rows = list(
        db.scalars(select(Content).where(Content.type == "novel").order_by(Content.id))
    )
    print(f"小说条目共 {len(rows)} 条；{'写入模式' if args.apply else '预览模式（加 --apply 写库）'}")

    print("（提示：非策展条目需联网判定，服务器通常不通 Bangumi -> 会显示为待定）")
    counts = {DECISION_SET: 0, DECISION_DROP: 0, DECISION_UNDECIDED: 0}
    dropped: list[int] = []
    for row in rows:
        decision, book_kind = decide_for(row, decisions, fetcher)
        counts[decision] += 1
        if decision == DECISION_DROP:
            dropped.append(row.id)
            print(f"  [删除] #{row.id} bgm={row.bangumi_id} {row.title[:40]} —— {DROP_REASON}")
            continue
        if decision == DECISION_UNDECIDED:
            print(f"  [待定] #{row.id} bgm={row.bangumi_id} {row.title[:40]} —— 无策展项且拉不到数据")
            continue
        label = {BOOK_KIND_LIGHT_NOVEL: "轻小说", BOOK_KIND_LITERATURE: "文学"}.get(book_kind, "-")
        same = row.book_kind == book_kind
        tag = "已标注" if same else ("标注" if args.apply else "将标注")
        if not same and args.apply:
            row.book_kind = book_kind
            db.commit()
        print(f"  [{label}] #{row.id} bgm={row.bangumi_id} {row.title[:40]} —— {tag}")

    if dropped and args.apply:
        removed = crud.delete_contents(db, dropped)
        print(f"已删除 {removed} 条非日本语种小说条目")

    print(
        f"合计：标注 {counts[DECISION_SET]} / 删除 {counts[DECISION_DROP]} / 待定 {counts[DECISION_UNDECIDED]}"
    )
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
