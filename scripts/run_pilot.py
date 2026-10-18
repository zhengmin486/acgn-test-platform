"""试点执行器：从选品到上线的单命令流水线。

把四步串起来，每步落盘中间产物，断了能从断点续跑：

    1. select  选品（scripts/select_hot.collect_candidates + allocate）
    2. push    本机抓详情 payload → POST /api/ingest/subjects 入库
    3. covers  本机按分层下载封面（S/A/B 全下，C 层跳过）→ 打包 tar
    4. upload  自主传文件通道上传 covers.tar.gz → 服务器解压 → 回填 image_url

为什么这样切：服务器没有境外出网（bgm 全系不可达），所以**抓取和封面下载
都只能在本机**；入库走 ingest API；封面文件走 recv.py 通道上传，比逐条 API
上传省一个数量级请求。

用法（本机，需 BANGUMI_PROXY 指向可用代理）：
    python scripts/run_pilot.py --stage all --total 100
    python scripts/run_pilot.py --stage push     # 断点续跑：只重推未入库的
    python scripts/run_pilot.py --stage covers   # 只重跑封面
产物都在 .workbuddy/pilot/ 下：selection.json / payloads/ / covers/ / covers.tar.gz
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_bangumi import build_opener  # noqa: E402
from scripts.select_hot import (  # noqa: E402
    allocate,
    collect_candidates,
)

WORK = PROJECT_ROOT / ".workbuddy" / "pilot"
INGEST_PATH = "/api/ingest/subjects"
BATCH = 50

DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")


def log(msg: str) -> None:
    print(msg, flush=True)


def stage_select(args) -> list[dict]:
    """选品并落盘 selection.json；已有文件且未 --refresh 直接复用（断点续跑）。"""
    out = WORK / "selection.json"
    if out.is_file() and not args.refresh:
        log(f"[select] 复用已有 {out}")
        return json.loads(out.read_text(encoding="utf-8"))

    proxy = args.proxy or os.environ.get("BANGUMI_PROXY") or None
    opener = build_opener(proxy)
    log(f"[select] 选品（pages={args.pages}, limit={args.limit}）...")
    report = collect_candidates(
        pages_per_category=args.pages, limit=args.limit, opener=opener
    )
    picked = allocate(report, total=args.total)
    log(
        f"[select] 候选 {len(report.candidates)}，选中 {len(picked)}"
        f"（详情失败 {len(report.detail_failures)}）"
    )
    payload = [
        {
            "bangumi_id": c.bangumi_id,
            "content_type": c.content_type,
            "tier": c.tier,
            "collect": c.collect,
            "score": c.score,
            "title": c.title,
            "need_cover_now": c.need_cover_now,
        }
        for c in picked
    ]
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[select] 清单已写 {out}")
    return payload


def login(base_url: str, username: str, password: str) -> dict[str, str]:
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


def stage_push(args, selection: list[dict]) -> None:
    """抓详情 payload 并推 ingest API。本地不落库。"""
    payloads_dir = WORK / "payloads"
    payloads_dir.mkdir(parents=True, exist_ok=True)
    proxy = args.proxy or os.environ.get("BANGUMI_PROXY") or None
    opener = build_opener(proxy)

    from scripts.select_hot import fetch_detail

    headers = login(args.ingest_url, args.username, args.password)
    key = args.ingest_key or os.environ.get("INGEST_API_KEY") or ""
    if not key:
        raise RuntimeError("缺少 INGEST_API_KEY")

    batch: list[dict] = []
    pushed = failed = 0
    for index, row in enumerate(selection, start=1):
        bid = row["bangumi_id"]
        cache = payloads_dir / f"{bid}.json"
        if cache.is_file():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            try:
                payload = fetch_detail(bid, opener=opener)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断
                log(f"  #{bid} 抓取失败：{exc}")
                failed += 1
                continue
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            time.sleep(0.35)
        batch.append(payload)

        if len(batch) >= BATCH or index == len(selection):
            body = json.dumps({"subjects": batch, "source": "pilot"}).encode()
            request = urllib.request.Request(
                f"{args.ingest_url.rstrip('/')}{INGEST_PATH}",
                data=body,
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "X-Ingest-Key": key,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        request, timeout=120
                    )
                ) as resp:
                    result = json.loads(resp.read().decode())
                imported = result.get("imported", 0)
                pushed += imported
                log(f"  推送 {len(batch)} 条 → 新增 {imported}")
            except urllib.error.HTTPError as exc:
                log(f"  批次推送失败 HTTP {exc.code}：{exc.read()[:200]}")
                failed += len(batch)
            batch = []

    log(f"[push] 完成：入库新增 {pushed}，失败 {failed}")


def stage_covers(args, selection: list[dict]) -> None:
    """本机下载 S/A/B 层封面（medium 尺寸），打包 tar 等待上传。"""
    covers_dir = WORK / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    proxy = args.proxy or os.environ.get("BANGUMI_PROXY") or None
    opener = build_opener(proxy)

    need = [row for row in selection if row.get("need_cover_now")]
    log(f"[covers] 需批量下载 {len(need)} / {len(selection)} 部（C 层走按需/后续补传）")

    ok = skip = fail = 0
    for row in need:
        bid = row["bangumi_id"]
        ext = ".jpg"
        target = covers_dir / f"cover_{bid}{ext}"
        if target.is_file() and target.stat().st_size > 1024:
            skip += 1
            continue
        remote = row.get("image_medium") or ""
        if not remote:
            fail += 1
            continue
        try:
            request = urllib.request.Request(
                remote, headers={"User-Agent": "acgn-test-platform/0.1"}
            )
            with opener.open(request, timeout=25) as response:
                body = response.read(3 * 1024 * 1024)
            target.write_bytes(body)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            log(f"  #{bid} 封面下载失败：{exc}")
            fail += 1
        time.sleep(0.3)

    archive = WORK / "covers.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(covers_dir.iterdir()):
            tar.add(path, arcname=path.name)
    log(f"[covers] 下载 {ok}，已有跳过 {skip}，失败 {fail}；打包 → {archive}")


def stage_upload(args) -> None:
    """经 recv.py 通道上传 covers.tar.gz 并在服务器解压 + 回填 image_url。"""
    archive = WORK / "covers.tar.gz"
    if not archive.is_file():
        raise RuntimeError("先跑 covers 阶段生成 covers.tar.gz")
    md5 = subprocess.run(
        ["md5sum", str(archive)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    log(f"[upload] {archive.name} md5={md5}")

    recv = args.recv_source or PROJECT_ROOT / ".workbuddy" / "recv.py"
    commands = (
        "mkdir -p /opt/acgn-upload /tmp/pilot && "
        f"base64 -d > /tmp/pilot/recv.py <<'EOF'\n{recv.read_text(encoding='utf-8')}\nEOF\n"
        "sed -i 's|/opt/acgn-test-platform/|/opt/acgn-upload/|' /tmp/pilot/recv.py && "
        "cd /tmp/pilot && setsid nohup python3 recv.py >/dev/null 2>&1 < /dev/null & sleep 2"
    )
    run_tat(args.instance, commands)
    run_tat(args.instance, f"mkdir -p /opt/acgn-upload && ls -la /opt/acgn-upload")
    log("[upload] 请确认防火墙 39127 已开（用 lighthouse 工具），然后手动执行：")
    log(
        f"  curl --noproxy '*' -X PUT -H 'X-Token: k9Qm2xT7' "
        f"-T {archive} http://{args.host}:39127/covers.tar.gz"
    )
    log("[upload] 上传后跑：--stage finalize 完成解压与回填")


def stage_finalize(args) -> None:
    """服务器端收尾：校验 md5、解压到 covers/、跑回填脚本。"""
    md5 = subprocess.run(
        ["md5sum", str(WORK / "covers.tar.gz")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    script = f"""
import hashlib, tarfile, pathlib
data = pathlib.Path('/opt/acgn-upload/covers.tar.gz').read_bytes()
digest = hashlib.md5(data).hexdigest()
assert digest == '{md5}', f'md5 mismatch: {{digest}}'
print('md5 ok')
dest = pathlib.Path('/opt/acgn-test-platform/app/static/covers')
dest.mkdir(parents=True, exist_ok=True)
with tarfile.open('/opt/acgn-upload/covers.tar.gz') as tar:
    tar.extractall(dest)
print('extracted:', len(list(dest.glob('cover_*'))))
"""
    run_tat(args.instance, f"base64 -d > /tmp/pilot/finalize.py <<'EOF'\n{script}\nEOF\npython3 /tmp/pilot/finalize.py")
    run_tat(
        args.instance,
        "cd /opt/acgn-test-platform && .venv/bin/python scripts/backfill_covers_from_dir.py --apply",
    )


def run_tat(instance: str, command: str) -> None:
    """提示用户用 lighthouse 工具执行；本脚本不直接调 MCP，只打印命令。"""
    log(f"[TAT] {instance} $ {command[:200]}{'...' if len(command) > 200 else ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="试点导入流水线")
    parser.add_argument(
        "--stage",
        default="all",
        choices=["all", "select", "push", "covers", "upload", "finalize"],
    )
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0, help="详情请求上限（试点控制规模）")
    parser.add_argument("--refresh", action="store_true", help="强制重新选品")
    parser.add_argument("--proxy", default=None)
    parser.add_argument(
        "--ingest-url",
        default=os.environ.get("INGEST_URL") or "http://154.8.194.198:8000",
    )
    parser.add_argument("--ingest-key", default=os.environ.get("INGEST_API_KEY") or "")
    parser.add_argument("--username", default=DEFAULT_ADMIN_USERNAME)
    parser.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD") or "")
    parser.add_argument("--instance", default="lhins-lunllslo")
    parser.add_argument("--host", default="154.8.194.198")
    parser.add_argument("--recv-source", default=None, help="recv.py 路径")
    args = parser.parse_args(argv)

    WORK.mkdir(parents=True, exist_ok=True)
    stages = (
        ["select", "push", "covers"] if args.stage == "all" else [args.stage]
    )
    selection: list[dict] = []
    if args.stage in ("all", "select", "push", "covers"):
        selection = stage_select(args)

    for stage in stages:
        if stage == "select":
            continue
        if stage == "push":
            stage_push(args, selection)
        elif stage == "covers":
            stage_covers(args, selection)
        elif stage == "upload":
            stage_upload(args)
        elif stage == "finalize":
            stage_finalize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
