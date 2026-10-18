"""随机导入作品测试：随机偏移翻 Bangumi 排行榜页抽样并导入。

覆盖：
- 排行榜单页解析（subject_id / id 回退 / 结构异常 / total / nsfw 过滤）；
- random_import：本地查重不发包、nsfw 统计、部分成功（榜单太小 / 页数预算）、
  随机偏移触达深页、偏移减半回退、连续失败收手、多页抽样、count=0 短路；
- 路由：游客守卫、管理员可开、预览→确认写入锁定同一批 ids（不重新随机）、
  网络不可用 / 无新作品两种错误分支。

网络与真实库均通过注入桩函数隔离：fetch_page / fetch_subject 由测试提供，
apply 路由下的 run_import 也用桩替换，避免触网或写开发库。
"""

import json
import random

import pytest
from auth_helpers import create_accounts, login_as_admin
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from scripts import import_bangumi
from scripts.import_bangumi import (
    ImportSummary,
    RankPage,
    RandomImportResult,
    SubjectOutcome,
    _fetch_rank_page,
    _map_and_store,
    random_import,
)

# ---------- 网络桩 ----------


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class _FakeOpener:
    """按调用次数返回不同排行榜页（与 URL / 偏移无关，只数调用）。"""

    def __init__(self, pages):
        self._pages = list(pages)
        self._calls = 0

    def open(self, request, timeout=None):
        idx = min(self._calls, len(self._pages) - 1)
        self._calls += 1
        payload = {"list": [{"subject_id": sid} for sid in self._pages[idx]]}
        return _FakeResp(json.dumps(payload).encode("utf-8"))


def _fake_subject(subject_id: int) -> dict:
    """构造一个可被 map_subject 接受的 Bangumi 条目 JSON。"""
    return {
        "id": subject_id,
        "type": 2,
        "name_cn": f"随机作品 {subject_id}",
        "name": f"Work {subject_id}",
        "summary": "用于随机导入测试的简介。",
        "rating": {"score": 7.5},
        "tags": [{"name": "热血", "count": 10}],
        "images": {"large": "http://example.com/cover.jpg"},
    }


def _make_session_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, expire_on_commit=False)
    db = S()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    return S


class _PageSource:
    """按 (type, offset) 提供排行榜页的 fetch_page 桩。

    - pages 里没显式给的偏移，默认返回 offset+1 .. offset+limit 的连续 ID
      （不同偏移不重叠）；值可以是 ID 列表，也可以是现成的 RankPage；
    - total 控制榜单总量（决定可抽的随机偏移范围）；
    - fail_above 模拟 API 拒绝深偏移（offset > fail_above 一律抛 BangumiAPIError）；
    - 记录全部调用与成功调用，供断言。
    """

    def __init__(self, pages=None, total=300, fail_above=None, limit=30):
        self.pages = dict(pages or {})
        self.total = total
        self.fail_above = fail_above
        self.limit = limit
        self.calls = []
        self.ok_calls = []

    def __call__(self, subject_type, offset, *, limit=30):
        self.calls.append((subject_type, offset))
        if self.fail_above is not None and offset > self.fail_above:
            raise import_bangumi.BangumiAPIError(
                f"HTTP 400 拒绝深偏移（offset={offset}）"
            )
        page = self.pages.get((subject_type, offset))
        if page is None:
            page = RankPage(ids=list(range(offset + 1, offset + limit + 1)))
        elif not isinstance(page, RankPage):
            page = RankPage(ids=list(page))
        page.total = self.total
        self.ok_calls.append((subject_type, offset))
        return page


def _no_sleep(seconds):
    """测试里不真正 sleep，翻页间歇会被频繁触发。"""


# ---------- 排行榜解析 ----------


class TestFetchRankPage:
    def test_parses_subject_id(self):
        opener = _FakeOpener([[1, 2, 3]])
        page = _fetch_rank_page(2, "rank", opener=opener)
        assert page.ids == [1, 2, 3]

    def test_falls_back_to_id_field(self):
        resp = _FakeResp(json.dumps({"list": [{"id": 9}, {"subject_id": 10}]}).encode())
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        page = _fetch_rank_page(2, "rank", opener=opener)
        assert page.ids == [9, 10]

    def test_missing_list_raises(self):
        resp = _FakeResp(json.dumps({"total": 0}).encode())
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        with pytest.raises(import_bangumi.BangumiAPIError):
            _fetch_rank_page(2, "rank", opener=opener, retries=0)

    def test_non_dict_payload_raises(self):
        resp = _FakeResp(b"not json")
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        with pytest.raises(import_bangumi.BangumiAPIError):
            _fetch_rank_page(2, "rank", opener=opener, retries=0)

    def test_total_parsed(self):
        body = {"list": [{"subject_id": 1}], "total": 1234}
        resp = _FakeResp(json.dumps(body).encode())
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        page = _fetch_rank_page(2, "rank", opener=opener)
        assert page.total == 1234

    def test_invalid_total_treated_as_missing(self):
        body = {"list": [{"subject_id": 1}], "total": "1234"}
        resp = _FakeResp(json.dumps(body).encode())
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        page = _fetch_rank_page(2, "rank", opener=opener)
        assert page.total is None

    def test_nsfw_items_filtered(self):
        body = {
            "list": [
                {"subject_id": 1},
                {"subject_id": 2, "subject": {"nsfw": True}},
                {"subject_id": 3, "nsfw": True},
                {"subject_id": 4, "subject": {"nsfw": False}},
                {"subject_id": 5, "subject": {}},
            ],
            "total": 100,
        }
        resp = _FakeResp(json.dumps(body).encode())
        opener = type("O", (), {"open": lambda self, request, timeout=None: resp})()
        page = _fetch_rank_page(2, "rank", opener=opener)
        assert page.ids == [1, 4, 5]
        assert page.nsfw_skipped == 2


# ---------- random_import ----------


class TestRandomImport:
    def test_preview_does_not_write(self):
        factory = _make_session_factory()
        db0 = factory()
        try:
            before = crud.count_contents(db0)
        finally:
            db0.close()
        source = _PageSource(total=300)
        res = random_import(
            session_factory=factory,
            count=10,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(42),
            sleep=_no_sleep,
        )
        assert res.pages == 1  # 10 条只需一页
        assert res.candidates == 30
        assert len(res.sampled) == 10
        assert res.summary.imported == 0
        assert res.summary.preview == 10
        db1 = factory()
        try:
            assert crud.count_contents(db1) == before  # 预览不写库
        finally:
            db1.close()

    def test_apply_writes(self):
        factory = _make_session_factory()
        db0 = factory()
        try:
            before = crud.count_contents(db0)
        finally:
            db0.close()
        res = random_import(
            session_factory=factory,
            count=8,
            subject_types=(2,),
            apply=True,
            fetch_page=_PageSource(total=300),
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(7),
            sleep=_no_sleep,
        )
        assert res.pages == 1
        assert len(res.sampled) == 8
        assert res.summary.imported == 8
        db1 = factory()
        try:
            assert crud.count_contents(db1) == before + 8
        finally:
            db1.close()

    def test_existing_ids_skipped_before_fetch(self):
        """已入库的 bangumi_id 在选中阶段就被本地过滤，不进抽样也不发详情请求。"""
        factory = _make_session_factory()
        db = factory()
        try:
            outcome = _map_and_store(
                db, _fake_subject(1), apply=True, cover_downloader=False
            )
            assert outcome.status == "imported"
        finally:
            db.close()

        fetched = []

        def fetch_subject(subject_id):
            fetched.append(subject_id)
            return _fake_subject(subject_id)

        source = _PageSource(total=300)  # 首页默认给 1..30
        res = random_import(
            session_factory=factory,
            count=10,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=fetch_subject,
            cover_downloader=False,
            rng=random.Random(3),
            sleep=_no_sleep,
        )
        assert 1 not in res.sampled
        assert res.duplicates == 1
        assert res.candidates == 29
        assert 1 not in fetched  # 详情请求也没为已入库条目发过

    def test_nsfw_stats_accumulated(self):
        factory = _make_session_factory()
        source = _PageSource(
            pages={(2, 0): RankPage(ids=list(range(1, 31)), nsfw_skipped=2)},
            total=30,  # 榜单只有一页
        )
        res = random_import(
            session_factory=factory,
            count=5,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(1),
            sleep=_no_sleep,
        )
        assert res.nsfw_skipped == 2
        assert len(res.sampled) == 5

    def test_small_pool_partial_result(self):
        """榜单总量不足一页时只翻一次，抽样按实际候选数收场。"""
        factory = _make_session_factory()
        source = _PageSource(total=30)
        res = random_import(
            session_factory=factory,
            count=100,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(1),
            sleep=_no_sleep,
        )
        assert res.pages == 1
        assert len(res.sampled) == 30
        assert res.rank_total == 30

    def test_page_budget_caps_requests(self):
        """候选始终凑不够时，翻页数被 RANDOM_PAGE_BUDGET 封顶，不死循环。"""
        factory = _make_session_factory()

        def fetch_page(subject_type, offset, *, limit=30):
            # 任何偏移都返回同一页：fresh 永远只有 30 个
            return RankPage(ids=list(range(1, 31)), total=300)

        res = random_import(
            session_factory=factory,
            count=100,
            subject_types=(2,),
            apply=False,
            fetch_page=fetch_page,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(1),
            sleep=_no_sleep,
        )
        assert res.pages == import_bangumi.RANDOM_PAGE_BUDGET
        assert len(res.sampled) == 30

    def test_offset_halving_fallback(self):
        """深偏移被 API 拒绝时逐步减半回退，最终在可用范围内成功。"""
        factory = _make_session_factory()
        source = _PageSource(total=300, fail_above=60)
        res = random_import(
            session_factory=factory,
            count=100,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(42),
            sleep=_no_sleep,
        )
        assert res.pages >= 1
        assert res.sampled
        # 尝试过深偏移（至少一次 > 60 的失败探测）
        assert any(offset > 60 for _, offset in source.calls)
        # 成功的页全部落在可用范围内
        assert all(offset <= 60 for _, offset in source.ok_calls)

    def test_consecutive_failures_abort(self):
        """连 offset=0 都连续失败（网络不可用）时收手，返回空结果不硬凑。"""

        def fetch_page(subject_type, offset, *, limit=30):
            raise import_bangumi.BangumiAPIError("网络错误：无法连接")

        factory = _make_session_factory()
        res = random_import(
            session_factory=factory,
            count=10,
            subject_types=(2,),
            apply=True,
            fetch_page=fetch_page,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(1),
            sleep=_no_sleep,
        )
        assert res.pages == 0
        assert res.sampled == []
        assert res.summary.requested == 10

    def test_random_offset_reaches_deep_pages(self):
        """total 足够大时，随机偏移会翻到头部以外的深页。"""
        factory = _make_session_factory()
        source = _PageSource(total=3000)
        res = random_import(
            session_factory=factory,
            count=45,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(5),
            sleep=_no_sleep,
        )
        assert res.pages == 3  # ceil(45/15)
        assert any(offset > 0 for _, offset in source.ok_calls)
        assert res.rank_total == 3000

    def test_multi_page_sampling_for_diversity(self):
        """条数较多时按 RANDOM_PAGE_SHARE 拆多页抽样，而不是单页全包。"""
        factory = _make_session_factory()
        source = _PageSource(total=300)
        res45 = random_import(
            session_factory=factory,
            count=45,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(2),
            sleep=_no_sleep,
        )
        assert len(source.ok_calls) == 3  # ceil(45/15) = 3 页
        assert len(res45.sampled) == 45

        source10 = _PageSource(total=300)
        res10 = random_import(
            session_factory=factory,
            count=10,
            subject_types=(2,),
            apply=False,
            fetch_page=source10,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(2),
            sleep=_no_sleep,
        )
        assert len(source10.ok_calls) == 1  # 10 条一页就够
        assert len(res10.sampled) == 10

    def test_both_types_used(self):
        """all 类型时两个类型都会翻页（各自先取首页学习 total）。"""
        factory = _make_session_factory()
        source = _PageSource(total=300)
        res = random_import(
            session_factory=factory,
            count=45,
            subject_types=(2, 4),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            rng=random.Random(11),
            sleep=_no_sleep,
        )
        types_called = {t for t, _ in source.ok_calls}
        assert types_called == {2, 4}
        assert len(res.sampled) == 45

    def test_count_zero_short_circuits(self):
        """count=0 时不发任何请求。"""
        factory = _make_session_factory()
        source = _PageSource(total=300)
        res = random_import(
            session_factory=factory,
            count=0,
            subject_types=(2,),
            apply=False,
            fetch_page=source,
            fetch_subject=_fake_subject,
            cover_downloader=False,
            sleep=_no_sleep,
        )
        assert source.calls == []
        assert res.pages == 0
        assert res.sampled == []


# ---------- 路由 ----------


@pytest.fixture
def random_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'random.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    create_accounts(TestingSession)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _empty_summary(requested: int) -> ImportSummary:
    return ImportSummary(
        requested=requested, fetched=0, imported=0, preview=0,
        duplicates=0, skipped=0, failed=0, outcomes=[],
    )


class TestRandomImportRoute:
    def test_guest_redirected(self, random_env):
        resp = random_env.get("/manage/import-random")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_admin_can_open(self, random_env, monkeypatch):
        login_as_admin(random_env)
        resp = random_env.get("/manage/import-random")
        assert resp.status_code == 200
        assert "随机导入作品" in resp.text
        assert 'name="count"' in resp.text
        assert 'name="rtype"' in resp.text

    def test_invalid_count_redirected(self, random_env, monkeypatch):
        login_as_admin(random_env)
        resp = random_env.post(
            "/manage/import-random",
            data={"action": "preview", "count": "999", "rtype": "all"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/manage/import-random?error=")

    def test_preview_then_apply_locks_same_ids(self, random_env, monkeypatch):
        login_as_admin(random_env)

        sampled = [11, 22, 33, 44]
        preview_summary = ImportSummary(
            requested=4, fetched=4, imported=0, preview=4,
            duplicates=0, skipped=0, failed=0,
            outcomes=[SubjectOutcome(bangumi_id=s, status="preview",
                                     title=f"作品{s}") for s in sampled],
        )
        monkeypatch.setattr(
            "app.content_admin_pages.random_import",
            lambda **kw: RandomImportResult(
                requested=4, sampled=sampled, summary=preview_summary,
                pages=2, candidates=50, duplicates=3, nsfw_skipped=1,
                rank_total=300),
        )

        resp = random_env.post(
            "/manage/import-random",
            data={"action": "preview", "count": "4", "rtype": "anime"},
        )
        assert resp.status_code == 200
        assert "同步结果" in resp.text
        # 随机翻页统计如实回显
        assert "翻页 2" in resp.text
        assert "新候选 50" in resp.text
        assert "跳过已入库 3" in resp.text
        # 预览锁定的 ids 通过隐藏字段回传
        assert f'value="{",".join(map(str, sampled))}"' in resp.text
        assert "作品11" in resp.text

        # 确认写入：run_import 必须收到与预览完全相同的 ids，且不重新随机
        captured = {}

        def fake_run_import(bangumi_ids, **kw):
            captured["ids"] = list(bangumi_ids)
            captured["apply"] = kw.get("apply")
            return ImportSummary(
                requested=len(bangumi_ids), fetched=len(bangumi_ids),
                imported=len(bangumi_ids), preview=0, duplicates=0,
                skipped=0, failed=0,
                outcomes=[SubjectOutcome(bangumi_id=s, status="imported",
                                         title=f"作品{s}") for s in bangumi_ids],
            )

        monkeypatch.setattr("app.content_admin_pages.run_import", fake_run_import)
        resp2 = random_env.post(
            "/manage/import-random",
            data={"action": "apply", "count": "4", "rtype": "anime",
                  "ids": ",".join(map(str, sampled))},
        )
        assert resp2.status_code == 200
        assert captured["apply"] is True
        assert captured["ids"] == sampled
        assert "正式写入" in resp2.text

    def test_apply_without_ids_redirected(self, random_env, monkeypatch):
        login_as_admin(random_env)
        resp = random_env.post(
            "/manage/import-random",
            data={"action": "apply", "rtype": "all", "ids": ""},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/manage/import-random?error=")

    def test_preview_network_error_redirected(self, random_env, monkeypatch):
        """一页都没翻到（网络不可用）时给出明确错误而非空结果页。"""
        login_as_admin(random_env)
        monkeypatch.setattr(
            "app.content_admin_pages.random_import",
            lambda **kw: RandomImportResult(
                requested=4, sampled=[], summary=_empty_summary(4), pages=0),
        )
        resp = random_env.post(
            "/manage/import-random",
            data={"action": "preview", "count": "4", "rtype": "anime"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/manage/import-random?error=")

    def test_preview_no_new_works_redirected(self, random_env, monkeypatch):
        """翻到页但候选全已入库时，提示换类型或稍后再试。"""
        login_as_admin(random_env)
        monkeypatch.setattr(
            "app.content_admin_pages.random_import",
            lambda **kw: RandomImportResult(
                requested=4, sampled=[], summary=_empty_summary(4),
                pages=2, candidates=0, duplicates=60),
        )
        resp = random_env.post(
            "/manage/import-random",
            data={"action": "preview", "count": "4", "rtype": "anime"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/manage/import-random?error=")
