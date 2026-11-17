"""重查询防滥用：并发闸门 + 每 IP 速率限制。

背景（2026-11 OOM 事故）：爬虫代理池轮换省份 IP，洪泛
``/contents?all_tags=1&tags=<随机组合>&year=<随机年>``——参数几乎不重复，
按“同一 URL/参数”去重或按单 IP 限流都挡不住（每个 IP 只来一两次）。该端点是
全站最重请求（全量标签计数 + 多标签 AND + 60 条渲染，实测 ~2.4s），并发堆积
会把单 worker 顶到 1.3G，触发 OOM → 整机 thrashing → sshd/nginx 全不响应。

防护思路：
- **全局并发闸门**（asyncio.Semaphore）：不管多少 IP，同时执行的重查询数有硬上限。
  这是对抗“代理池轮换”的核心——并发不随 IP 数量增长。
- **每 IP 速率窗口**：拦单 IP 高频（同一出口 IP 快速连打），辅助手段。
- **只作用于标记的重查询路径**（默认 ``/contents?all_tags=1``），正常浏览不受影响。
- 超限返回 429 + Retry-After，快速失败，绝不排队堆积内存。

全部走 ``settings`` 配置，可整体关闭。单进程内有状态（与项目“uvicorn 单 worker”
前提一致）；多 worker 部署需换进程外限流（见模块末尾说明）。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from app.config import settings


class _SlidingWindow:
    """每 IP 滑动窗口计数器（单调递增时间戳队列，纯内存）。"""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float, now: float) -> bool:
        q = self._hits[key]
        cutoff = now - window
        # 弹出窗口外的旧记录
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._hits.clear()


_window = _SlidingWindow()
_gate: asyncio.Lock | None = None
_active = 0  # 当前在执行的重查询数


def _get_gate() -> asyncio.Lock:
    """惰性创建锁（事件循环建立后才能创建，避免 import 期绑定错误的 loop）。"""
    global _gate
    if _gate is None:
        _gate = asyncio.Lock()
    return _gate


def _client_ip(request: Request) -> str:
    """取客户端 IP。信任 X-Forwarded-For 首段（部署在 nginx 反代后时）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def is_heavy_request(request: Request) -> bool:
    """是否为需要限流的重查询。默认匹配 /contents + all_tags=1。"""
    if request.url.path.rstrip("/") != "/contents":
        return False
    all_tags = request.query_params.get("all_tags", "").lower()
    return all_tags in ("1", "true", "yes", "on")


def reset_state() -> None:
    """清空限流状态（供测试用）。"""
    global _window, _active
    _window = _SlidingWindow()
    _active = 0


async def anti_abuse_middleware(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    """对重查询做并发闸门 + 每 IP 速率限制，超限快速返回 429。"""
    if not settings.abuse_enabled or not is_heavy_request(request):
        return await call_next(request)

    now = time.monotonic()
    ip = _client_ip(request)
    # 每 IP 速率窗口（先做，廉价：不占用并发槽位就能挡掉单 IP 高频）
    if not _window.allow(ip, settings.abuse_per_ip_limit, settings.abuse_window_seconds, now):
        return Response(
            content="Too Many Requests",
            status_code=429,
            headers={"Retry-After": str(settings.abuse_window_seconds)},
        )

    # 全局并发闸门：用锁保护计数，超上限立即 429（不排队，避免堆积内存）
    global _active
    gate = _get_gate()
    async with gate:
        if _active >= max(1, settings.abuse_heavy_concurrency):
            return Response(
                content="Too Many Requests",
                status_code=429,
                headers={"Retry-After": "5"},
            )
        _active += 1
    try:
        return await call_next(request)
    finally:
        async with gate:
            _active -= 1


# 多 worker 说明：本模块用进程内信号量 + 内存窗口，与项目“uvicorn 单 worker”前提
# 一致。若将来改成 --workers N，每个 worker 会各持一份限额（总阈值 = N × 配置值），
# 届时应替换为 Redis 等进程外限流，或前置 nginx limit_req / limit_conn。
