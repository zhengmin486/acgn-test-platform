"""把 select_from_archive 产出的 payload 清单直接推给 ingest API。

与 run_pilot.py 的 push 阶段的区别：归档路线的 payload **已经构造完毕**
（含 summary / rating / tags / infobox），不需要再逐条抓详情——推就完事了。
这也是官方归档路线相对 API 路线的最大优势：一次下载替代一千次详情请求。

分批推送（默认 50 条/批），单批失败不影响其余批次；响应里逐条结果落盘
便于对账。本机不落库，数据只进服务器。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

INGEST_PATH = "/api/ingest/subjects"
BATCH = 50


def login(base_url: str, username: str, password: str) -> dict[str, str]:
    """用口令换 session Cookie（与 run_pilot 同一套语义）。"""
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        if "login" in response.geturl():
            raise RuntimeError("登录失败：用户名或口令不正确")
        cookie = response.headers.get("Set-Cookie") or ""
    session = cookie.split(";")[0] if cookie else ""
    if not session:
        raise RuntimeError("登录失败：响应里没有 session Cookie")
    return {"Cookie": session}


def push(
    payloads: list[dict],
    *,
    base_url: str,
    key: str,
    headers: dict[str, str],
    source: str,
) -> tuple[int, int, int]:
    """分批推送，返回 (总条数, imported, duplicate)。"""
    plain_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    total = imported = duplicate = 0
    for start in range(0, len(payloads), BATCH):
        batch = payloads[start : start + BATCH]
        body = json.dumps({"subjects": batch, "source": source}).encode()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{INGEST_PATH}",
            data=body,
            headers={**headers, "Content-Type": "application/json", "X-Ingest-Key": key},
            method="POST",
        )
        try:
            with plain_opener.open(request, timeout=180) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            print(f"  批次 [{start}:{start+len(batch)}] HTTP {exc.code}：{exc.read()[:200]}")
            continue
        total += result.get("total", 0)
        imported += result.get("imported", 0)
        duplicate += sum(1 for r in result.get("results", []) if r.get("status") == "duplicate")
        skipped = sum(1 for r in result.get("results", []) if r.get("status") == "skipped")
        print(
            f"  批次 [{start}:{start+len(batch)}] 新增 {imported} 累计 / "
            f"重复 {duplicate} / 跳过 {skipped}",
        )
        if start + BATCH < len(payloads):
            time.sleep(1.0)
    return total, imported, duplicate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推送归档选品 payload 到服务器")
    parser.add_argument(
        "--selection",
        default=str(PROJECT_ROOT / ".workbuddy/pilot/selection.json"),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("INGEST_URL") or "http://154.8.194.198:8000",
    )
    parser.add_argument("--key", default=os.environ.get("INGEST_API_KEY") or "")
    parser.add_argument("--username", default=os.environ.get("ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD") or "")
    parser.add_argument("--source", default="archive-dump")
    args = parser.parse_args(argv)

    if not args.key:
        print("缺少 INGEST_API_KEY", file=sys.stderr)
        return 1
    if not args.password:
        print("缺少 ADMIN_PASSWORD", file=sys.stderr)
        return 1

    payloads = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    if not payloads:
        print("清单为空，没有可推送的内容", file=sys.stderr)
        return 1
    print(f"推送 {len(payloads)} 条到 {args.base_url} ...")

    headers = login(args.base_url, args.username, args.password)
    total, imported, duplicate = push(
        payloads,
        base_url=args.base_url,
        key=args.key,
        headers=headers,
        source=args.source,
    )
    print(f"完成：共 {total}，新增 {imported}，重复 {duplicate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())