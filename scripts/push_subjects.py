"""抓取端：本机抓 Bangumi → 直接推送服务器入库，本地不留任何业务数据。

为什么是这个形态（2026-10-18 定型）：
    服务器没有境外出网，api.bgm.tv 在那边根本不可达；而本机有代理、出口 IP 干净。
    于是把职责切开——**抓取在本机、入库在服务器**：
      本机：只把 Bangumi 原始 JSON 放进内存，POST 给 /api/ingest/subjects；
      服务器：复用既有 ``_map_and_store`` 做映射 / 查重 / 标签 / 关系 / 入库。
    本机自始至终不建库、不写 SQLite，所以「真实词条只在服务器」是结构上成立的，
    而不是靠自觉不导入。

前置条件：
    服务器 .env 配了 INGEST_API_KEY，且本机同名环境变量一致（或用 --ingest-key 传入）。
    服务器 .env 的 DEFAULT_ADMIN_* 账号用于登录（抓取端拿 Cookie 证明是站长）。

用法示例：
    # 抓 3 部，推到线上
    python scripts/push_subjects.py --id 265 975 1029 --ingest-url http://154.8.194.198:8000

    # 从文件读 ID（一行一个）
    python scripts/push_subjects.py --id-file ids.txt

    # 只抓取不推送，看看会拿到什么（连服务器都不需要）
    python scripts/push_subjects.py --id 265 --dry-run

    # 走代理抓（默认读 BANGUMI_PROXY）
    BANGUMI_PROXY=http://127.0.0.1:9910 python scripts/push_subjects.py --id 265

退出码：0 全部成功或已存在；1 有条目失败；2 参数/配置错误（URL、Key、登录态缺失）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_bangumi import (  # noqa: E402
    BangumiAPIError,
    build_opener,
    fetch_subject,
    resolve_proxy,
)

# 服务器端点与单批上限须与 app/ingest_api.py 一致。
INGEST_PATH = "/api/ingest/subjects"
BATCH_SIZE = 200
DEFAULT_TIMEOUT = 20.0


def parse_ids(values: list[str], id_file: str | None) -> list[int]:
    """合并命令行 ID 与文件里的 ID，去重保序，忽略非法项。"""
    raw = list(values)
    if id_file:
        text = Path(id_file).read_text(encoding="utf-8")
        raw.extend(text.replace(",", " ").replace("\n", " ").split())
    ids: list[int] = []
    for item in raw:
        token = str(item).strip()
        if token.isdigit() and int(token) not in ids:
            ids.append(int(token))
    return ids


def chunked(items: list, size: int):
    """按固定大小切片（顺序保留）。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def login(base_url: str, username: str, password: str, timeout: float) -> dict[str, str]:
    """登录服务器拿 session Cookie，返回请求头字典（抓取端需要站长登录态）。

    走真实登录表单而不是绕过鉴权：ingest 端点既要 Key 也要站长角色，
    两者都要，所以这里老老实实用账号密码换 Cookie。
    """
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if "login" in final_url:
                raise RuntimeError("登录失败：用户名或口令不正确")
            cookie = response.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"登录失败：HTTP {exc.code}") from exc
    session = cookie.split(";")[0] if cookie else ""
    if not session:
        raise RuntimeError("登录失败：响应里没有 session Cookie")
    return {"Cookie": session}


def push_batch(
    base_url: str,
    ingest_key: str,
    headers: dict[str, str],
    subjects: list[dict],
    timeout: float,
) -> dict:
    """把一批 payload POST 给服务器，返回响应 JSON。"""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{INGEST_PATH}",
        data=json.dumps({"subjects": subjects, "source": "local-fetch"}).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
            "X-Ingest-Key": ingest_key,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"入库接口返回 HTTP {exc.code}：{detail}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"入库接口返回的不是 JSON：{exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本机抓取 Bangumi 条目并推送到服务器入库（本地不写数据库）",
    )
    parser.add_argument("--id", nargs="*", default=[], help="Bangumi 条目 ID（空格分隔）")
    parser.add_argument("--id-file", help="从文件读 ID（一行一个，或用逗号/空格分隔）")
    parser.add_argument(
        "--ingest-url",
        default=os.environ.get("INGEST_URL") or "http://154.8.194.198:8000",
        help="服务器地址（默认读 INGEST_URL）",
    )
    parser.add_argument(
        "--ingest-key",
        default=os.environ.get("INGEST_API_KEY") or "",
        help="入库 API Key；默认读 INGEST_API_KEY 环境变量",
    )
    parser.add_argument("--username", default=os.environ.get("ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD") or "")
    parser.add_argument("--proxy", default=None, help="Bangumi 抓取代理；默认读 BANGUMI_PROXY")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sleep", type=float, default=1.0, help="每次请求间隔秒数，默认 1")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="只抓取并打印，不推送")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ids = parse_ids(args.id, args.id_file)
    if not ids:
        print("没有可处理的 ID（用 --id 或 --id-file 指定）", file=sys.stderr)
        return 2

    proxy = resolve_proxy(args.proxy)
    print(f"待抓取 {len(ids)} 个条目")
    print(f"抓取代理：{proxy or '未启用（直连）'}")
    print(f"目标服务器：{args.ingest_url}")

    if args.dry_run:
        print("模式：--dry-run，只抓取不推送。")
    elif not args.ingest_key:
        print("缺少入库 Key：请设置 INGEST_API_KEY 或 --ingest-key", file=sys.stderr)
        return 2

    opener = build_opener(proxy)
    payloads: list[dict] = []
    fetch_failed: list[tuple[int, str]] = []
    for index, subject_id in enumerate(ids, start=1):
        try:
            payloads.append(fetch_subject(subject_id, opener=opener, timeout=args.timeout))
        except BangumiAPIError as exc:
            fetch_failed.append((subject_id, str(exc)))
            print(f"  抓取失败 #{subject_id}：{exc}", file=sys.stderr)
        if args.sleep > 0 and index < len(ids):
            import time

            time.sleep(args.sleep)

    print(f"抓取完成：成功 {len(payloads)} 个，失败 {len(fetch_failed)} 个")
    if args.dry_run:
        for item in payloads[:5]:
            print(f"  #{item.get('id')} {item.get('name_cn') or item.get('name')}")
        return 1 if fetch_failed else 0

    try:
        headers = login(args.ingest_url, args.username, args.password, args.timeout)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("已登录服务器（站长登录态就绪）。")

    imported = 0
    api_failed = 0
    for batch in chunked(payloads, max(1, args.batch_size)):
        try:
            result = push_batch(args.ingest_url, args.ingest_key, headers, batch, args.timeout)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        imported += int(result.get("imported", 0))
        for row in result.get("results", []):
            if row.get("status") == "failed":
                api_failed += 1
                print(f"  入库失败 #{row.get('bangumi_id')}：{row.get('reason')}", file=sys.stderr)
        print(f"  推送 {len(batch)} 个 → 新增 {result.get('imported', 0)} 个")

    print(f"完成：新增入库 {imported} 个，抓取失败 {len(fetch_failed)} 个，入库失败 {api_failed} 个")
    print("本地未写入任何数据（抓取端不建库）。")
    return 1 if (fetch_failed or api_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
