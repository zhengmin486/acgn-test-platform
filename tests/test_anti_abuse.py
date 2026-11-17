"""重查询防滥用测试：标签计数 TTL 缓存 + 并发闸门 + 每 IP 速率限制。

背景：2026-11 代理池洪泛 /contents?all_tags=1（2.4s/次）顶爆单 worker 内存。
- 缓存层：list_tags_with_counts 命中 TTL 后不再查库；标签变更即失效；不同口径不串。
- 限流层：全局并发闸门（不随 IP 轮换失效）+ 每 IP 速率窗口；超限 429 + Retry-After。

用临时 SQLite + 直接调用中间件函数，与开发库隔离、并发可控。
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import anti_abuse, crud
from app.config import settings
from app.database import Base
from app.models import Content


# ---------- 缓存层 ----------


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """独立临时库 + 一部带标签作品；清空缓存并缩短 TTL 可控。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cache.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    db.add(Content(title="甲", type="anime", summary="s", rating=8.0))
    db.commit()
    crud.replace_content_tags(db, 1, ["机甲", "热血"])
    db.close()
    monkeypatch.setattr(crud, "TAG_COUNT_TTL_SECONDS", 300)
    crud._tag_count_cache.clear()
    yield Session, engine
    crud._tag_count_cache.clear()


class TestTagCountCache:
    def test_repeat_call_uses_cache(self, cache_env, monkeypatch):
        """TTL 内重复调用命中缓存：第二次不执行 SQL。"""
        Session, _ = cache_env
        db = Session()
        first = crud.list_tags_with_counts(db, limit=None)
        assert first == [("机甲", 1), ("热血", 1)]

        # 记录一次 execute：命中缓存时不应触发
        calls = []
        orig = db.execute

        def tracking(*a, **kw):
            calls.append(1)
            return orig(*a, **kw)

        monkeypatch.setattr(db, "execute", tracking)
        second = crud.list_tags_with_counts(db, limit=None)
        assert second == first
        assert calls == [], "缓存命中不该查库"
        db.close()

    def test_invalidate_on_tag_change(self, cache_env):
        """标签变更后 invalidate_tag_index 清空缓存，下次查到新值。"""
        Session, _ = cache_env
        db = Session()
        assert crud.list_tags_with_counts(db, limit=None) == [("机甲", 1), ("热血", 1)]
        crud.replace_content_tags(db, 1, ["治愈"])
        # 走 replace_content_tags 已触发 invalidate
        assert crud.list_tags_with_counts(db, limit=None) == [("治愈", 1)]
        db.close()

    def test_different_params_not_shared(self, cache_env):
        """不同 limit / content_type / exclude 的缓存互不串。"""
        Session, _ = cache_env
        db = Session()
        full = crud.list_tags_with_counts(db, limit=None)
        limited = crud.list_tags_with_counts(db, limit=1)
        assert full == [("机甲", 1), ("热血", 1)]
        assert limited == [("机甲", 1)]
        scoped = crud.list_tags_with_counts(db, content_type="game", limit=None)
        assert scoped == []
        db.close()


# ---------- 限流层 ----------


def make_request(path="/contents?all_tags=1", ip="1.2.3.4", xff=None):
    """构造中间件所需的最小 Request（query_params / headers / client）。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path.split("?", 1)[0],
        "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
        "headers": [],
        "client": (ip, 12345),
    }
    if xff:
        scope["headers"] = [(b"x-forwarded-for", xff.encode())]
    return Request(scope)


async def ok_call_next(request):
    from starlette.responses import Response

    return Response("ok", status_code=200)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_abuse(monkeypatch):
    """每个限流测试重置状态 + 固定配置。"""
    anti_abuse.reset_state()
    monkeypatch.setattr(settings, "abuse_enabled", True)
    monkeypatch.setattr(settings, "abuse_heavy_concurrency", 2)
    monkeypatch.setattr(settings, "abuse_per_ip_limit", 6)
    monkeypatch.setattr(settings, "abuse_window_seconds", 60)
    yield
    anti_abuse.reset_state()


class TestHeavyDetection:
    def test_heavy_only_contents_all_tags(self):
        assert anti_abuse.is_heavy_request(make_request("/contents?all_tags=1"))
        assert anti_abuse.is_heavy_request(make_request("/contents?all_tags=true"))
        assert not anti_abuse.is_heavy_request(make_request("/contents"))
        assert not anti_abuse.is_heavy_request(make_request("/?all_tags=1"))
        assert not anti_abuse.is_heavy_request(make_request("/api/contents?all_tags=1"))

    def test_client_ip_from_xff(self):
        assert anti_abuse._client_ip(make_request(ip="1.1.1.1", xff="9.9.9.9, 1.1.1.1")) == "9.9.9.9"
        assert anti_abuse._client_ip(make_request(ip="1.1.1.1")) == "1.1.1.1"


class TestRateLimit:
    def test_per_ip_window_blocks_excess(self):
        """单 IP 窗口内超过 per_ip_limit → 429。"""
        results = [
            run(anti_abuse.anti_abuse_middleware(make_request(ip="5.5.5.5"), ok_call_next))
            for _ in range(8)
        ]
        codes = [r.status_code for r in results]
        assert codes[:6] == [200] * 6
        assert codes[6:] == [429, 429]
        assert results[-1].headers.get("Retry-After") == "60"

    def test_different_ips_independent(self):
        """不同 IP 各自计数，互不占用（轮换 IP 单靠速率挡不住——由并发闸门兜底）。"""
        for i in range(6):
            r = run(anti_abuse.anti_abuse_middleware(make_request(ip=f"10.0.0.{i}"), ok_call_next))
            assert r.status_code == 200

    def test_disabled_passes_all(self, monkeypatch):
        monkeypatch.setattr(settings, "abuse_enabled", False)
        for _ in range(20):
            r = run(anti_abuse.anti_abuse_middleware(make_request(ip="5.5.5.5"), ok_call_next))
            assert r.status_code == 200


class TestConcurrencyGate:
    def test_concurrent_heavy_capped(self):
        """轮换 IP 并发打重查询：同时执行数不超过上限，多余立即 429。"""
        release = asyncio.Event()
        entered = []
        entered_lock = asyncio.Lock()

        async def slow_call_next(request):
            async with entered_lock:
                entered.append(1)
            await release.wait()  # 阻塞住，模拟 2.4s 重查询
            from starlette.responses import Response

            return Response("ok", status_code=200)

        async def fire(n):
            req = make_request(ip=f"172.16.0.{n}")  # 每个不同 IP，绕开速率限制
            return await anti_abuse.anti_abuse_middleware(req, slow_call_next)

        async def scenario():
            tasks = [asyncio.create_task(fire(i)) for i in range(6)]
            await asyncio.sleep(0.05)  # 让前几个进入 slow_call_next 阻塞
            # 放行所有被阻塞的请求，等全部任务完成
            release.set()
            final = await asyncio.gather(*tasks)
            return len(entered), [r.status_code for r in final]

        entered_count, codes = run(scenario())
        assert entered_count == 2, "同时执行的重查询应被限制为 2"
        assert codes.count(200) == 2
        assert codes.count(429) == 4
