"""从 anibt 镜像站批量补封面：API 镜像拿地址，图片镜像落盘。

服务器没有境外出网（lain.bgm.tv 不可达），但 bgmimg.anibt.net / bgmapi.anibt.net
是国内可达的 lain 结构镜像（实测 2026-10-18）。流程：
    1. 取库里所有无本地封面且有 bangumi_id 的条目；
    2. GET {api镜像}/v0/subjects/{id} 拿 images（medium 优先，与站内偏好一致）；
    3. 域名替换为图片镜像后下载，魔术字节校验；
    4. 落盘 cover_{bangumi_id}.{ext} + 生成 thumb/full 派生图；
    5. 回填 image_url，顺手清 remote_image_url。

幂等：已有本地封面跳过；失败记录到失败清单（不重排队列），重跑只补缺。
限速：每请求间 sleep（默认 0.25s），1953 部约 15 分钟。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select

from app import crud, media
from app.config import settings
from app.database import SessionLocal
from app.models import Content

API_BASE = "https://bgmapi.anibt.net"
IMG_HOST = "https://bgmimg.anibt.net"
UA = {"User-Agent": "acgn-test-platform/0.1 (cover backfill via mirror)"}
MAX_BYTES = 5 * 1024 * 1024
SIZE_PREFERENCE = ("medium", "common", "large")


def http_json(subject_id, retries=2):
    url = f"{API_BASE}/v0/subjects/{subject_id}"
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0)
    raise RuntimeError(f"API {subject_id}: {last}")


def http_image(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read(MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0)
    raise RuntimeError(f"IMG {url}: {last}")


def pick_image(images):
    if not isinstance(images, dict):
        return None
    for size in SIZE_PREFERENCE:
        url = images.get(size)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    # 兼容 //lain.bgm.tv/... 协议相对写法
    for size in SIZE_PREFERENCE:
        url = images.get(size)
        if isinstance(url, str) and url.startswith("//"):
            return "https:" + url
    return None


def mirror_url(remote):
    path = urllib.parse.urlsplit(remote).path
    query = urllib.parse.urlsplit(remote).query
    suffix = ("?" + query) if query else ""
    return IMG_HOST + path + suffix


def run(limit, sleep_s, apply):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Content.id, Content.bangumi_id)
            .where(Content.image_url.is_(None))
            .where(Content.bangumi_id.is_not(None))
            .order_by(Content.id)
            .limit(limit)
        ).all()
        total = db.scalar(
            select(func.count(Content.id))
            .where(Content.image_url.is_(None))
            .where(Content.bangumi_id.is_not(None))
        )
        print(f"待补 {len(rows)} / 共 {total} 条", flush=True)
    finally:
        db.close()

    ok = skip = fail = 0
    failures = []
    root = media.cover_root()
    root.mkdir(parents=True, exist_ok=True)
    for index, (content_id, bangumi_id) in enumerate(rows, start=1):
        try:
            payload = http_json(bangumi_id)
            remote = pick_image(payload.get("images"))
            if not remote:
                fail += 1
                failures.append((bangumi_id, "no image in payload"))
                continue
            body = http_image(mirror_url(remote))
            if not body or len(body) > MAX_BYTES or media.sniff_image(body) is None:
                fail += 1
                failures.append((bangumi_id, "bad image body"))
                continue
            suffix = ".jpg"
            path_part = urllib.parse.urlsplit(remote).path.lower()
            for ext in (".png", ".webp", ".gif"):
                if path_part.endswith(ext):
                    suffix = ext
                    break
            name = f"cover_{bangumi_id}{suffix}"
            original = root / name
            media._atomic_write(original, body)
            for kind in ("thumb", "full"):
                try:
                    media.generate_variant(kind, original)
                except media.MediaError:
                    pass
            if apply:
                db2 = SessionLocal()
                try:
                    content = db2.get(Content, content_id)
                    if content is not None:
                        content.image_url = f"/covers/{name}"
                        content.remote_image_url = None
                        db2.commit()
                finally:
                    db2.close()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            failures.append((bangumi_id, str(exc)[:80]))
        if index % 50 == 0:
            print(f"  进度 {index}/{len(rows)} ok={ok} fail={fail}", flush=True)
        time.sleep(sleep_s)

    print(f"完成：成功 {ok}，失败 {fail}", flush=True)
    if failures:
        with open(PROJECT_ROOT / "cover_failures.json", "w", encoding="utf-8") as f:
            json.dump(failures, f, ensure_ascii=False, indent=1)
        print(f"失败清单已写 cover_failures.json（前 5: {failures[:5]}）")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.limit, args.sleep, args.apply)


if __name__ == "__main__":
    main()
