"""特殊分类档期页测试：按播出/上映日期分组排版 + 票房展示 + 排序。

覆盖：
- app.business_pages._group_by_date：ISO 日期分组、倒序、季度描述原文组、
  「时间待定」排最后、组名/周几/距今天数派生；
- _group_by_date(by_month=True)：剧场类两分类（ova_sp / cn_movie）按月分组
  （用户 2026-10-24 要求），月组不派生周几/距今天数，未上线新番仍按日；
- crud.list_schedules / list_all_schedules：特殊分类按 air_date 倒序，
  季度档期排序不受影响（零回归）；
- crud.ensure_schedule_time_columns：历史库幂等补 air_date / box_office 列；
- 页面：/schedule?category= 渲染日期分组组头与票房徽标；
- upsert_schedule：air_date / box_office 参与比对与更新。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app.business_pages import _group_by_date
from app.database import Base, build_engine, get_db
from app.main import app
from app.models import AnimeSchedule
from auth_helpers import create_accounts, login_as_admin


@pytest.fixture()
def session_factory(tmp_path):
    """受控临时库：只建表，不播种真实数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'date.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def add_entry(session_factory, **overrides) -> AnimeSchedule:
    """写入一条特殊分类档期记录（字段可覆盖），返回该记录。"""
    data = {
        "season_code": "cn_movie",
        "source_key": "样例",
        "title_main": "样例",
        "source_url": "https://yuc.wiki/movie/",
        "category": "cn_movie",
    }
    data.update(overrides)
    db = session_factory()
    try:
        record = AnimeSchedule(**data)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    finally:
        db.close()


class TestGroupByDate:
    """_group_by_date：分组键、排序与组头派生信息。"""

    def test_iso_dates_grouped_desc(self, session_factory):
        """air_date 分组：新的日期在前，同组多条保持传入顺序。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11"),
            add_entry(session_factory, source_key="b", title_main="乙", air_date="2025-11-14"),
            add_entry(session_factory, source_key="c", title_main="丙", air_date="2026-12-11"),
            add_entry(session_factory, source_key="d", title_main="丁", air_date="2026-12-05"),
        ]
        groups = _group_by_date(rows)
        assert [g["name"] for g in groups] == [
            "2026年12月11日",
            "2026年12月5日",
            "2025年11月14日",
        ]
        assert [w.title_main for w in groups[0]["works"]] == ["甲", "丙"]

    def test_quarter_note_group_and_unknown_last(self, session_factory):
        """只有季度描述（2027冬）按原文归组；无日期无说明的「时间待定」排最后。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11"),
            add_entry(session_factory, source_key="b", title_main="乙", broadcast_note="2027冬"),
            add_entry(session_factory, source_key="c", title_main="丙", broadcast_note="2027冬"),
            add_entry(session_factory, source_key="d", title_main="丁"),
        ]
        groups = _group_by_date(rows)
        assert [g["name"] for g in groups] == ["2027冬", "2026年12月11日", "时间待定"]
        assert [w.title_main for w in groups[0]["works"]] == ["乙", "丙"]

    def test_group_header_extras(self, session_factory):
        """ISO 日期组派生周几与距今天数；原文组没有这些信息。"""
        target = date.today()
        iso = f"{target.year:04d}-{target.month:02d}-{target.day:02d}"
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date=iso),
            add_entry(session_factory, source_key="b", title_main="乙", broadcast_note="2027冬"),
        ]
        groups = {g["name"]: g for g in _group_by_date(rows)}
        iso_group = groups[f"{target.year}年{target.month}月{target.day}日"]
        assert iso_group["countdown"] == "今天"
        assert iso_group["weekday_name"] in {"周一", "周二", "周三", "周四", "周五", "周六", "周日"}
        quarter_group = groups["2027冬"]
        assert quarter_group["weekday_name"] is None
        assert quarter_group["countdown"] is None


class TestGroupByMonth:
    """_group_by_date(by_month=True)：剧场类两分类按月分组（2026-10-24 用户要求）。

    电影一部一个上映日，按日分组会碎成大量单条组（线上 cn_movie 76 部 71 组、
    ova_sp 363 部 238 组）；按月归拢后组头形如「2026年11月」，当月片单一目了然。
    """

    def test_iso_dates_grouped_by_month_desc(self, session_factory):
        """同月的多条并成一组，组内沿用传入顺序；月组新的在前。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11"),
            add_entry(session_factory, source_key="b", title_main="乙", air_date="2025-11-14"),
            add_entry(session_factory, source_key="c", title_main="丙", air_date="2026-12-05"),
            add_entry(session_factory, source_key="d", title_main="丁", air_date="2025-11-30"),
        ]
        groups = _group_by_date(rows, by_month=True)
        assert [g["name"] for g in groups] == ["2026年12月", "2025年11月"]
        assert [w.title_main for w in groups[0]["works"]] == ["甲", "丙"]
        assert [w.title_main for w in groups[1]["works"]] == ["乙", "丁"]

    def test_month_group_has_no_weekday_or_countdown(self, session_factory):
        """月组不派生周几与距今天数（月份不是时间点，模板据此隐藏 pill）。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11")
        ]
        group = _group_by_date(rows, by_month=True)[0]
        assert group["name"] == "2026年12月"
        assert group["weekday_name"] is None
        assert group["countdown"] is None

    def test_note_fallback_and_unknown_last(self, session_factory):
        """无 air_date 时仍按 note 原文归组；时间待定排最后。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11"),
            add_entry(session_factory, source_key="b", title_main="乙", broadcast_note="2027冬"),
            add_entry(session_factory, source_key="c", title_main="丙"),
        ]
        groups = _group_by_date(rows, by_month=True)
        assert [g["name"] for g in groups] == ["2027冬", "2026年12月", "时间待定"]

    def test_day_granularity_is_still_the_default(self, session_factory):
        """默认仍按日：未上线新番（unreleased）不受按月改动影响。"""
        rows = [
            add_entry(session_factory, source_key="a", title_main="甲", air_date="2026-12-11")
        ]
        assert _group_by_date(rows)[0]["name"] == "2026年12月11日"


class TestScheduleOrdering:
    """查询排序：特殊分类按 air_date 倒序，季度档期维持星期升序。"""

    def test_category_orders_by_air_date_desc(self, session_factory):
        add_entry(session_factory, source_key="a", title_main="甲", air_date="2025-11-14")
        add_entry(session_factory, source_key="b", title_main="乙", air_date="2026-12-11")
        add_entry(session_factory, source_key="c", title_main="丙")  # 无日期排最后
        db = session_factory()
        try:
            total, items = crud.list_schedules(db, category="cn_movie")
            assert total == 3
            assert [i.title_main for i in items] == ["乙", "甲", "丙"]
            all_items = crud.list_all_schedules(db, category="cn_movie")
            assert [i.title_main for i in all_items] == ["乙", "甲", "丙"]
        finally:
            db.close()

    def test_season_order_unchanged(self, session_factory):
        """季度档期仍按星期升序（本次改动的零回归护栏）。"""
        db = session_factory()
        try:
            for key, weekday, title in [
                ("sun", 7, "周日番"),
                ("mon", 1, "周一番"),
                ("none", None, "待定番"),
            ]:
                db.add(
                    AnimeSchedule(
                        season_code="202610",
                        source_key=key,
                        title_main=title,
                        weekday=weekday,
                        source_url="https://yuc.wiki/202610/",
                    )
                )
            db.commit()
            _, items = crud.list_schedules(db, season="202610")
            assert [i.title_main for i in items] == ["周一番", "周日番", "待定番"]
        finally:
            db.close()


class TestEnsureScheduleTimeColumns:
    """ensure_schedule_time_columns：历史库幂等补列。"""

    def test_adds_columns_idempotently(self, tmp_path):
        # 先建一张「缺列」的老表，再走 ensure
        engine = build_engine(f"sqlite:///{tmp_path / 'old.db'}")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE anime_schedules ("
                    "id INTEGER PRIMARY KEY, season_code VARCHAR(20), source_key VARCHAR(200),"
                    "title_main VARCHAR(200), source_url VARCHAR(500),"
                    "last_synced_at DATETIME)"
                )
            )
        added = crud.ensure_schedule_time_columns(engine)
        assert set(added) == {"air_date", "box_office"}
        columns = {c["name"] for c in inspect(engine).get_columns("anime_schedules")}
        assert {"air_date", "box_office"} <= columns
        # 再跑一次：无新增
        assert crud.ensure_schedule_time_columns(engine) == []
        engine.dispose()

    def test_missing_table_is_noop(self, tmp_path):
        engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        assert crud.ensure_schedule_time_columns(engine) == []
        engine.dispose()


class TestUpsertScheduleTimeFields:
    """upsert_schedule：air_date / box_office 参与比对与更新。"""

    def test_box_office_updated_on_resync(self, session_factory):
        db = session_factory()
        try:
            base = {
                "season_code": "cn_movie",
                "source_key": "鬼灭",
                "title_main": "鬼灭之刃 无限城篇",
                "source_url": "https://yuc.wiki/movie/",
                "category": "cn_movie",
                "broadcast_note": "2025/11/14开始上映",
                "air_date": "2025-11-14",
                "box_office": "内地总票房￥6.78亿",
            }
            _, action = crud.upsert_schedule(db, dict(base))
            assert action == "added"
            # 票房变化 -> updated
            changed = dict(base, box_office="内地总票房￥7.1亿")
            _, action = crud.upsert_schedule(db, changed)
            assert action == "updated"
            record = db.scalar(
                crud.select(AnimeSchedule).where(AnimeSchedule.source_key == "鬼灭")
            )
            assert record.box_office == "内地总票房￥7.1亿"
            assert record.air_date == "2025-11-14"
        finally:
            db.close()


# ---------- 页面渲染 ----------


@pytest.fixture()
def date_client(tmp_path):
    """临时库：大陆引进剧场版三条（含票房），返回已登录客户端。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'page.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    create_accounts(factory, admin=True, user=False)
    db = factory()
    try:
        db.add_all(
            [
                AnimeSchedule(
                    season_code="cn_movie",
                    source_key="a",
                    title_main="鬼灭之刃 无限城篇",
                    source_url="https://yuc.wiki/movie/",
                    category="cn_movie",
                    broadcast_note="2025/11/14开始上映",
                    air_date="2025-11-14",
                    box_office="内地总票房￥6.78亿",
                ),
                AnimeSchedule(
                    season_code="cn_movie",
                    source_key="b",
                    title_main="某未来电影",
                    source_url="https://yuc.wiki/movie/",
                    category="cn_movie",
                    broadcast_note="2027冬",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client = TestClient(app)
    app.dependency_overrides[get_db] = override_get_db
    login_as_admin(client)
    yield client
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_category_page_renders_date_groups(date_client):
    """分类页默认视图按日期分组：组头是日期（而非星期），票房原文展示。

    大陆引进剧场版（cn_movie）属剧场类，分组到月：组头是「2025年11月」而非
    具体到日，也没有距今天数 pill（月份不是时间点）。
    """
    resp = date_client.get("/schedule?category=cn_movie")
    assert resp.status_code == 200
    assert "2027冬" in resp.text
    assert "2025年11月" in resp.text
    # 按月分组：不再出现具体到日的组头与倒计时 pill
    assert "2025年11月14日" not in resp.text
    assert "schedule-day-countdown" not in resp.text
    assert "内地总票房￥6.78亿" in resp.text


@pytest.fixture()
def ova_client(tmp_path):
    """临时库：剧场版/OVA/特别篇两条（同月 + 不同月），返回已登录客户端。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'ova.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    create_accounts(factory, admin=True, user=False)
    db = factory()
    try:
        db.add_all(
            [
                AnimeSchedule(
                    season_code="ova_sp",
                    source_key="a",
                    title_main="某OVA前篇",
                    source_url="https://yuc.wiki/sp/",
                    category="ova_sp",
                    air_date="2026-11-27",
                ),
                AnimeSchedule(
                    season_code="ova_sp",
                    source_key="b",
                    title_main="某OVA后篇",
                    source_url="https://yuc.wiki/sp/",
                    category="ova_sp",
                    air_date="2026-11-06",
                ),
                AnimeSchedule(
                    season_code="ova_sp",
                    source_key="c",
                    title_main="某旧剧场版",
                    source_url="https://yuc.wiki/sp/",
                    category="ova_sp",
                    air_date="2024-07-19",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client = TestClient(app)
    app.dependency_overrides[get_db] = override_get_db
    login_as_admin(client)
    yield client
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_ova_sp_page_renders_month_groups(ova_client):
    """剧场版/OVA/特别篇页同样按月分组（两页剧场版，2026-10-24 用户要求）。"""
    resp = ova_client.get("/schedule?category=ova_sp")
    assert resp.status_code == 200
    # 同月两条并成一组，组头只到月
    assert "2026年11月" in resp.text
    assert "2026年11月27日" not in resp.text
    assert "2024年7月" in resp.text
    assert "2024年7月19日" not in resp.text
    assert "schedule-day-countdown" not in resp.text


@pytest.fixture()
def unreleased_client(tmp_path):
    """临时库：未上线新番两条，返回已登录客户端。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'unreleased.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    create_accounts(factory, admin=True, user=False)
    db = factory()
    try:
        db.add_all(
            [
                AnimeSchedule(
                    season_code="unreleased",
                    source_key="a",
                    title_main="某未上线新番",
                    source_url="https://yuc.wiki/202701/",
                    category="unreleased",
                    air_date="2027-01-06",
                ),
                AnimeSchedule(
                    season_code="unreleased",
                    source_key="b",
                    title_main="某待定新番",
                    source_url="https://yuc.wiki/202701/",
                    category="unreleased",
                    broadcast_note="2027冬",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client = TestClient(app)
    app.dependency_overrides[get_db] = override_get_db
    login_as_admin(client)
    yield client
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_unreleased_page_stays_day_grouped(unreleased_client):
    """未上线新番仍按日分组 + 倒计时 pill（本次只改两页剧场版的零回归护栏）。"""
    resp = unreleased_client.get("/schedule?category=unreleased")
    assert resp.status_code == 200
    assert "2027年1月6日" in resp.text
    assert "schedule-day-countdown" in resp.text  # 距今天数 pill 仍在


def test_category_api_exposes_time_fields(date_client):
    """档期 API 暴露 air_date / box_office。"""
    body = date_client.get("/api/schedule", params={"category": "cn_movie"}).json()
    rows = {item["title_main"]: item for item in body["items"]}
    assert rows["鬼灭之刃 无限城篇"]["air_date"] == "2025-11-14"
    assert rows["鬼灭之刃 无限城篇"]["box_office"] == "内地总票房￥6.78亿"
    assert rows["某未来电影"]["air_date"] is None
