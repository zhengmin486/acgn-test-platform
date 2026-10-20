"""档期 → 作品库 自动补库（scripts/fill_schedule_works.py）测试。

覆盖：档期窗口计算、标题归一化、候选打分与置信阈值、条目解析、
已在库跳过、入库成功、解析不到、搜索异常熔断、预览不写库、基址取值。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AnimeSchedule, Content
from scripts import fill_schedule_works as fw

# 一条能被 _map_and_store 正常入库的最小动画条目。
PAYLOAD = {
    "id": 270274,
    "type": 2,
    "name": "ネコぱら",
    "name_cn": "猫娘乐园",
    "summary": "测试用简介",
    "rating": {"rank": 0, "score": 6.5, "total": 100},
    "tags": [{"name": "萌", "count": 10}],
    "infobox": [{"key": "放送开始", "value": "2020-01-09"}],
    "images": {"large": "https://example.invalid/a.jpg"},
}

CANDIDATE = {
    "id": 270274,
    "name": "ネコぱら",
    "name_cn": "猫娘乐园",
    "date": "2020-01-09",
    "type": 2,
}


@pytest.fixture()
def session_factory(tmp_path):
    """受控临时库：只建表，不播种真实数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fill.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def add_schedule(
    session_factory,
    *,
    season_code="202001",
    title_main="猫娘乐园",
    title_jp="ネコぱら",
    source_key=None,
) -> AnimeSchedule:
    db = session_factory()
    try:
        row = AnimeSchedule(
            season_code=season_code,
            source_key=source_key or f"{season_code}-{title_main}",
            title_main=title_main,
            title_cn=title_main,
            title_jp=title_jp,
            source_url=f"https://yuc.wiki/{season_code}/",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def add_content(session_factory, *, bangumi_id: int) -> Content:
    db = session_factory()
    try:
        row = Content(
            title="已存在作品",
            type="anime",
            summary="已存在",
            rating=7.0,
            bangumi_id=bangumi_id,
        )
        db.add(row)
        db.commit()
        return row
    finally:
        db.close()


def fake_searcher(results):
    def _search(keyword, *, opener=None, base_url="", timeout=0, retries=0, sleep=None):
        return list(results)

    return _search


def fake_fetcher(payload):
    def _fetch(subject_id, *, opener=None, timeout=0, retries=0, base_url="", sleep=None):
        return dict(payload)

    return _fetch


def raising_searcher(exc=None):
    def _search(keyword, *, opener=None, base_url="", timeout=0, retries=0, sleep=None):
        raise RuntimeError(exc or "搜索失败")

    return _search


def noop_sleep(_seconds):
    return None


# ---------- 档期窗口 ----------


def test_season_window_october_season():
    assert fw.season_window("202610") == (date(2026, 10, 1), date(2026, 12, 31))


def test_season_window_january_season():
    assert fw.season_window("202001") == (date(2020, 1, 1), date(2020, 3, 31))


def test_season_window_invalid_code():
    assert fw.season_window("") is None
    assert fw.season_window("abc") is None
    assert fw.season_window("2020") is None


# ---------- 标题归一化 ----------


def test_normalize_title_removes_spaces_and_punctuation():
    assert fw.normalize_title(" SHOW BY ROCK!! ") == "showbyrock"
    assert fw.normalize_title(None) == ""


# ---------- 候选打分 ----------


def test_score_exact_jp_plus_in_season_date():
    score = fw.score_candidate(
        CANDIDATE,
        jp=fw.normalize_title("ネコぱら"),
        cn=fw.normalize_title("猫娘乐园"),
        window=fw.season_window("202001"),
    )
    # 日文原名精确 100 + 中文名精确 80 + 播出日期在档期内 40
    assert score == 100 + 80 + 40


def test_score_exact_jp_without_date_hits():
    score = fw.score_candidate(
        {**CANDIDATE, "date": "2010-01-01"},
        jp=fw.normalize_title("ネコぱら"),
        cn=fw.normalize_title("猫娘乐园"),
        window=fw.season_window("202001"),
    )
    assert score == 180  # 只靠标题命中，日期不加分


def test_score_date_only_is_below_threshold():
    score = fw.score_candidate(
        {**CANDIDATE, "name": "别的作品", "name_cn": "别的"},
        jp=fw.normalize_title("ネコぱら"),
        cn=fw.normalize_title("猫娘乐园"),
        window=fw.season_window("202001"),
    )
    assert score == fw.SCORE_DATE_IN_SEASON
    assert score < fw.MIN_SCORE  # 仅日期命中不足以入库


def test_score_partial_title_plus_date_passes():
    score = fw.score_candidate(
        {**CANDIDATE, "name": "ネコぱら OVA"},
        jp=fw.normalize_title("ネコぱら"),
        cn=fw.normalize_title("猫娘乐园"),
        window=fw.season_window("202001"),
    )
    assert score >= fw.MIN_SCORE


# ---------- 条目解析 ----------


def test_resolve_subject_accepts_high_confidence(session_factory):
    entry = add_schedule(session_factory)
    subject_id, reason = fw.resolve_subject(
        entry,
        opener=None,
        base_url="https://example.invalid",
        sleep=noop_sleep,
        searcher=fake_searcher([CANDIDATE]),
    )
    assert subject_id == 270274
    assert "命中" in reason


def test_resolve_subject_rejects_low_confidence(session_factory):
    entry = add_schedule(session_factory)
    subject_id, reason = fw.resolve_subject(
        entry,
        opener=None,
        base_url="https://example.invalid",
        sleep=noop_sleep,
        searcher=fake_searcher([{**CANDIDATE, "name": "无关作品", "name_cn": "无关", "date": "2001-01-01"}]),
    )
    assert subject_id is None
    assert "置信不足" in reason or "未找到" in reason


def test_resolve_subject_no_candidates(session_factory):
    entry = add_schedule(session_factory)
    subject_id, reason = fw.resolve_subject(
        entry,
        opener=None,
        base_url="https://example.invalid",
        sleep=noop_sleep,
        searcher=fake_searcher([]),
    )
    assert subject_id is None
    assert "未找到" in reason


# ---------- 整轮补库 ----------


def test_fill_imports_missing_work(session_factory):
    add_schedule(session_factory)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=fake_searcher([CANDIDATE]),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.total == 1
    assert summary.imported == 1
    assert summary.already == 0
    assert summary.aborted == ""

    db = session_factory()
    try:
        assert db.query(Content).filter(Content.bangumi_id == 270274).count() == 1
    finally:
        db.close()


def test_fill_skips_work_already_in_library(session_factory):
    add_schedule(session_factory)
    add_content(session_factory, bangumi_id=270274)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=fake_searcher([CANDIDATE]),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.already == 1
    assert summary.imported == 0


def test_fill_counts_unresolved(session_factory):
    add_schedule(session_factory)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=fake_searcher([]),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.unresolved == 1
    assert summary.imported == 0


def test_fill_preview_does_not_write(session_factory):
    add_schedule(session_factory)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=False,
        sleep=noop_sleep,
        searcher=fake_searcher([CANDIDATE]),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.pending == 1
    assert summary.imported == 0

    db = session_factory()
    try:
        assert db.query(Content).count() == 0
    finally:
        db.close()


def test_fill_failure_is_isolated(session_factory):
    add_schedule(session_factory)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=raising_searcher(),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.failed == 1
    assert summary.imported == 0


def test_fill_aborts_after_consecutive_search_errors(session_factory):
    for index in range(fw.ABORT_AFTER_ERRORS):
        add_schedule(session_factory, title_main=f"作品{index}", title_jp=f"さくひん{index}")
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=raising_searcher("镜像不可达"),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert summary.failed == fw.ABORT_AFTER_ERRORS
    assert "连续" in summary.aborted


def test_fill_message_reports_counts(session_factory):
    add_schedule(session_factory)
    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202001",
        apply=True,
        sleep=noop_sleep,
        searcher=fake_searcher([CANDIDATE]),
        fetcher=fake_fetcher(PAYLOAD),
    )
    assert "202001" in summary.message
    assert "新增入库 1" in summary.message


# ---------- 接口基址 ----------


def test_resolve_base_url_prefers_env(monkeypatch):
    monkeypatch.setenv("BANGUMI_API_BASE", "https://custom.invalid/")
    assert fw.resolve_base_url() == "https://custom.invalid"


def test_resolve_base_url_falls_back_to_mirror(monkeypatch):
    monkeypatch.delenv("BANGUMI_API_BASE", raising=False)
    assert fw.resolve_base_url() == fw.DEFAULT_MIRROR
