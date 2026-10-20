"""拉取线上已有作品的 bangumi_id 集合（公开 API，无需凭据）。

用途：配合 scripts/select_new_works.py 做「只选未入库的作品」——避免把已经
在库里的 12000+ 部再推一遍（/api/ingest 虽然幂等跳过，但白白浪费请求，且
无法保证「净新增」数量）。

接口：GET /api/contents?page=N&page_size=500，响应 {total, items[], total_pages}；
每个 item 带 bangumi_id。逐页拉到 total_pages 为止，只抽取 bangumi_id 去重。

用法：
    python scripts/fetch_existing_ids.py
    python scripts/fetch_existing_ids.py --base-url http://154.8.194.198:8000 \
        --out .workbuddy/pilot/existing_ids.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENDPOINT = "/api/contents"


def fetch_all(base_url: str, page_size: int = 500, sleep=time.sleep) -> set[int]:
    """翻完 /api/contents 所有分页，返回 bangumi_id 集合。"""
    plain_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ids: set[int] = set()
    page = 1
    total_pages = None
    seen = 0
    while True:
        query = urllib.parse.urlencode({"page": page, "page_size": page_size})
        url = f"{base_url.rstrip('/')}{ENDPOINT}?{query}"
        try:
            with plain_opener.open(url, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"拉取第 {page} 页失败 HTTP {exc.code}：{exc.read()[:200]}")
        except Exception as exc:  # noqa: BLE001 - 网络抖动如实上报，便于重试
            raise RuntimeError(f"拉取第 {page} 页出错：{exc}")

        items = data.get("items") or []
        for item in items:
            bid = item.get("bangumi_id")
            if isinstance(bid, int) and not isinstance(bid, bool):
                ids.add(bid)
        seen += len(items)
        total_pages = data.get("total_pages") or total_pages
        total = data.get("total")
        print(f"  第 {page} 页：累计 {seen}/{total} 条，唯一 bangumi_id {len(ids)}")
        if not total_pages or page >= total_pages:
            break
        page += 1
        if page <= total_pages:
            sleep(0.2)
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="拉取线上已有作品的 bangumi_id 集合")
    parser.add_argument(
        "--base-url",
        default="http://154.8.194.198:8000",
        help="站点基址（默认生产服务器）",
    )
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument(
        "--out",
        default=str(PROJECT_ROOT / ".workbuddy/pilot/existing_ids.json"),
        help="输出 JSON 路径（存去重后的 bangumi_id 列表）",
    )
    args = parser.parse_args(argv)

    print(f"拉取 {args.base_url}{ENDPOINT} 已有作品 ID ...")
    ids = fetch_all(args.base_url, args.page_size)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")
    print(f"已写出 {len(ids)} 个已有 bangumi_id -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
