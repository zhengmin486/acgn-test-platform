"""档期 ↔ 作品库 关联测试（Bangumi subject_id 确定性关联 + 译名别名回填）。

覆盖：
- 数据层：ensure_schedule_link_columns 幂等补列；
- crud：标题索引构建、译名匹配优先级、别名合并去重、关联写入、
  将来收录的反查连通；
- 补库脚本：--link-only 关联回填（标题匹配命中 / 联网兜底 / 已在库跳过）、
  补库时顺带记录 subject_id 与建立关联；
- 入库管线：新作品入库后自动反查关联档期；
- 页面：档期卡片有关联时直跳作品详情页，无关联时退化为按标题搜索。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, build_engine, get_db
from app.main import app
from app.models import AnimeSchedule, Content
from auth_helpers import create_accounts, login_as_admin
from scripts import fill_schedule_works as fw
from scripts import import_bangumi as ib


@pytest.fixture()
def session_factory(tmp_path):
    """受控临时库：只建表，不播种真实数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'link.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def add_schedule(session_factory, **overrides) -> AnimeSchedule:
    """写入一条档期记录（字段可覆盖），返回该记录。"""
    data = {
        "season_code": "202610",
        "source_key": "样例-1",
        "title_main": "猫娘乐园",
        "title_cn": "猫娘乐园",
        "title_jp": "ネコぱら",
        "source_url": "https://yuc.wiki/202610/",
    }
    data.update(overrides)
    db = session_factory()
    try:
        row = AnimeSchedule(**data)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def add_content(session_factory, **overrides) -> Content:
    """写入一条作品记录（字段可覆盖），返回该记录。"""
    data = {
        "title": "猫娘乐园",
        "type": "anime",
        "summary": "简介",
        "rating": 7.0,
        "bangumi_id": 270274,
    }
    data.update(overrides)
    db = session_factory()
    try:
        row = Content(**data)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


# ---------- 数据层：幂等补列 ----------


def test_ensure_schedule_link_columns_adds_both_columns(tmp_path):
    """老库补列：两列都加上（含索引），且可重复执行（幂等）。"""
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    # 模拟老库：手写一张不含关联列的 anime_schedules
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE anime_schedules ("
                "id INTEGER PRIMARY KEY, "
                "season_code VARCHAR(20) NOT NULL, "
                "source_key VARCHAR(200) NOT NULL, "
                "title_main VARCHAR(200) NOT NULL"
                ")"
            )
        )

    added = crud.ensure_schedule_link_columns(engine)
    assert "bangumi_subject_id" in added
    assert "content_id" in added

    columns = {c["name"] for c in inspect(engine).get_columns("anime_schedules")}
    assert {"bangumi_subject_id", "content_id"} <= columns
    indexes = {i["name"] for i in inspect(engine).get_indexes("anime_schedules")}
    assert "ix_anime_schedules_bangumi_subject_id" in indexes
    assert "ix_anime_schedules_content_id" in indexes

    # 幂等：再跑一次不再补列
    assert crud.ensure_schedule_link_columns(engine) == []
    engine.dispose()


def test_ensure_schedule_link_columns_keeps_existing_values(tmp_path):
    """补列不破坏已有行数据。"""
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{tmp_path / 'old2.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE anime_schedules ("
                "id INTEGER PRIMARY KEY, "
                "season_code VARCHAR(20) NOT NULL, "
                "source_key VARCHAR(200) NOT NULL, "
                "title_main VARCHAR(200) NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO anime_schedules (season_code, source_key, title_main) "
                "VALUES ('202610', 'k1', '样例作品')"
            )
        )
    crud.ensure_schedule_link_columns(engine)
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT season_code, source_key, title_main FROM anime_schedules")
        ).fetchall()
    assert rows == [("202610", "k1", "样例作品")]
    engine.dispose()


def test_ensure_schedule_link_columns_skips_missing_table(tmp_path):
    """表还没建时直接返回空列表，不报错。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert crud.ensure_schedule_link_columns(engine) == []
    engine.dispose()


# ---------- crud：标题索引与译名匹配 ----------


def test_title_index_prefers_japanese_original(session_factory):
    """日文原名命中最强：档期 title_jp 对上作品的 title_original。"""
    add_content(
        session_factory,
        title="中文译名A",
        title_original="ネコぱら",
        bangumi_id=111,
    )
    add_schedule(session_factory, title_main="中文译名B", title_jp="ネコぱら")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        entry = db.query(AnimeSchedule).first()
        content = crud.match_content_for_schedule(db, entry, index)
        assert content is not None
        assert content.bangumi_id == 111
    finally:
        db.close()


def test_title_index_matches_chinese_title(session_factory):
    """档期中文名对上作品主标题也能命中。"""
    add_content(session_factory, title="猫娘乐园", bangumi_id=111)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        entry = db.query(AnimeSchedule).first()
        content = crud.match_content_for_schedule(db, entry, index)
        assert content is not None
        assert content.bangumi_id == 111
    finally:
        db.close()


def test_title_index_matches_alias(session_factory):
    """档期译名对上作品别名也能命中（别名兜底）。"""
    add_content(
        session_factory,
        title="中文译名A",
        aliases=["猫娘乐园"],
        bangumi_id=111,
    )
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        entry = db.query(AnimeSchedule).first()
        content = crud.match_content_for_schedule(db, entry, index)
        assert content is not None
        assert content.bangumi_id == 111
    finally:
        db.close()


def test_title_index_drops_ambiguous_keys(session_factory):
    """同一归一化标题对应多部作品时整键丢弃（宁缺勿错，不乱连）。"""
    add_content(session_factory, title="同名作品", bangumi_id=111)
    add_content(session_factory, title="同名作品", bangumi_id=222)
    add_schedule(session_factory, title_main="同名作品", title_jp="-Collections")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        assert "同名作品" not in index["title"]
        entry = db.query(AnimeSchedule).first()
        assert crud.match_content_for_schedule(db, entry, index) is None
    finally:
        db.close()


def test_match_returns_none_when_no_hit(session_factory):
    """完全对不上时返回 None（交给联网解析兜底）。"""
    add_content(session_factory, title="别的作品", bangumi_id=111)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        entry = db.query(AnimeSchedule).first()
        assert crud.match_content_for_schedule(db, entry, index) is None
    finally:
        db.close()


def test_title_index_ignores_non_anime_contents(session_factory):
    """同名的轻小说/游戏不算命中——档期页全是新番动画。"""
    add_content(session_factory, title="猫娘乐园", type="novel", bangumi_id=111)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        index = crud.build_schedule_title_index(db)
        assert "猫娘乐园" not in index["title"]
        entry = db.query(AnimeSchedule).first()
        assert crud.match_content_for_schedule(db, entry, index) is None
    finally:
        db.close()


# ---------- crud：别名合并与关联写入 ----------


def test_merge_content_aliases_adds_new_names(session_factory):
    """新译名并入 aliases；与 title 重复的不加。"""
    content = add_content(session_factory, title="猫娘乐园")
    db = session_factory()
    try:
        row = db.get(Content, content.id)
        merged = crud.merge_content_aliases(db, row, ["ネコぱら", "猫娘乐园", "  "])
        assert "ネコぱら" in merged
        assert "猫娘乐园" not in merged
        assert row.aliases == merged
    finally:
        db.close()


def test_merge_content_aliases_dedupes_and_caps(session_factory):
    """重复译名不叠加；超过上限时截断。"""
    content = add_content(session_factory, title="作品", aliases=["已有别名"])
    db = session_factory()
    try:
        row = db.get(Content, content.id)
        crud.merge_content_aliases(db, row, ["已有别名", "新别名"])
        assert row.aliases.count("已有别名") == 1
        assert "新别名" in row.aliases

        many = [f"别名{i}" for i in range(crud.MAX_CONTENT_ALIASES + 10)]
        merged = crud.merge_content_aliases(db, row, many)
        assert len(merged) <= crud.MAX_CONTENT_ALIASES
    finally:
        db.close()


def test_link_schedule_to_content_writes_both_columns(session_factory):
    """关联写入 content_id 与 bangumi_subject_id，并把档期译名并入别名。"""
    content = add_content(session_factory, title="中文名", bangumi_id=111)
    entry = add_schedule(session_factory, title_main="档期中文名", title_jp="ネコぱら")
    db = session_factory()
    try:
        row = db.get(AnimeSchedule, entry.id)
        target = db.get(Content, content.id)
        changed = crud.link_schedule_to_content(
            db,
            row,
            target,
            subject_id=111,
            alias_names=[row.title_main, row.title_cn, row.title_jp],
        )
        assert changed is True
        assert row.content_id == content.id
        assert row.bangumi_subject_id == 111
        assert "ネコぱら" in target.aliases
    finally:
        db.close()


def test_link_schedule_to_content_no_change_returns_false(session_factory):
    """已经关联好时返回 False（不重复写库）。"""
    content = add_content(session_factory, title="作品", bangumi_id=111)
    entry = add_schedule(session_factory)
    db = session_factory()
    try:
        row = db.get(AnimeSchedule, entry.id)
        target = db.get(Content, content.id)
        crud.link_schedule_to_content(db, row, target, subject_id=111)
        assert crud.link_schedule_to_content(db, row, target, subject_id=111) is False
    finally:
        db.close()


def test_link_schedules_by_subject_id_links_pending_rows(session_factory):
    """将来收录自动连通：反查 subject_id 命中的档期条目并补上 content_id。"""
    content = add_content(session_factory, title="作品", bangumi_id=111)
    entry = add_schedule(session_factory, title_jp="ネコぱら")
    db = session_factory()
    try:
        row = db.get(AnimeSchedule, entry.id)
        row.bangumi_subject_id = 111
        db.commit()

        target = db.get(Content, content.id)
        linked = crud.link_schedules_by_subject_id(db, 111, target)
        assert len(linked) == 1
        assert row.content_id == content.id
        assert "ネコぱら" in target.aliases
    finally:
        db.close()


def test_link_schedules_by_subject_id_without_subject_is_noop(session_factory):
    """没有 subject_id 时不查库、不改动。"""
    content = add_content(session_factory, title="作品", bangumi_id=111)
    db = session_factory()
    try:
        target = db.get(Content, content.id)
        assert crud.link_schedules_by_subject_id(db, None, target) == []
    finally:
        db.close()


def test_find_content_by_bangumi_id(session_factory):
    """按 Bangumi 条目 id 精确查作品（跨译名关联的基准）。"""
    content = add_content(session_factory, title="作品", bangumi_id=999)
    db = session_factory()
    try:
        found = crud.find_content_by_bangumi_id(db, 999)
        assert found is not None and found.id == content.id
        assert crud.find_content_by_bangumi_id(db, 12345) is None
        assert crud.find_content_by_bangumi_id(db, None) is None
    finally:
        db.close()


# ---------- 关联回填脚本 ----------


def test_link_only_matches_by_title_without_network(session_factory):
    """标题归一匹配即可关联，不联网。"""
    add_content(session_factory, title="猫娘乐园", title_original="ネコぱら", bangumi_id=270274)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    def _no_network(*args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("标题已命中，不应联网解析")

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=True,
        use_network=False,
        searcher=_no_network,
    )
    assert summary.total == 1
    assert summary.linked == 1
    assert summary.unmatched == 0

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.content_id is not None
        assert row.bangumi_subject_id == 270274
    finally:
        db.close()


def test_link_only_falls_back_to_network_resolution(session_factory):
    """标题匹配不上时走 Bangumi 解析兜底，作品库有则关联上。"""
    add_content(session_factory, title="完全不同的名字", bangumi_id=270274)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    candidate = {
        "id": 270274,
        "name": "ネコぱら",
        "name_cn": "猫娘乐园",
        "date": "2026-10-05",
        "type": 2,
    }

    def _search(keyword, **kwargs):
        return [candidate]

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=True,
        use_network=True,
        searcher=_search,
        sleep=lambda _s: None,
    )
    assert summary.linked == 1
    assert summary.unmatched == 0

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.content_id is not None
        assert row.bangumi_subject_id == 270274
    finally:
        db.close()


def test_link_only_records_subject_when_work_not_in_library(session_factory):
    """解析成功但作品库还没有：把 subject_id 记到档期行，等将来入库自动连通。"""
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    candidate = {
        "id": 424242,
        "name": "ネコぱら",
        "name_cn": "猫娘乐园",
        "date": "2026-10-05",
        "type": 2,
    }

    def _search(keyword, **kwargs):
        return [candidate]

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=True,
        use_network=True,
        searcher=_search,
        sleep=lambda _s: None,
    )
    assert summary.unmatched == 1
    assert summary.linked == 0

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.bangumi_subject_id == 424242
        assert row.content_id is None
    finally:
        db.close()


def test_link_only_skips_already_linked(session_factory):
    """已关联的条目跳过，不重复处理。"""
    content = add_content(session_factory, title="猫娘乐园", bangumi_id=270274)
    entry = add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        row = db.get(AnimeSchedule, entry.id)
        row.content_id = content.id
        db.commit()
    finally:
        db.close()

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=True,
        use_network=False,
    )
    assert summary.already == 1
    assert summary.linked == 0


def test_link_only_preview_does_not_write(session_factory):
    """预览模式不写库。"""
    add_content(session_factory, title="猫娘乐园", bangumi_id=270274)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=False,
        use_network=False,
    )
    assert summary.linked == 1

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.content_id is None
    finally:
        db.close()


def test_link_only_filters_by_season(session_factory):
    """限定档期码时只处理该季条目。"""
    add_content(session_factory, title="猫娘乐园", bangumi_id=270274)
    add_schedule(session_factory, season_code="202610", title_main="猫娘乐园")
    add_schedule(
        session_factory,
        season_code="202607",
        source_key="样例-2",
        title_main="猫娘乐园",
    )

    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        season_code="202610",
        apply=True,
        use_network=False,
    )
    assert summary.total == 1


def test_link_summary_message_reports_counts(session_factory):
    """结论行包含关键计数。"""
    add_content(session_factory, title="猫娘乐园", bangumi_id=270274)
    add_schedule(session_factory, title_main="猫娘乐园")
    summary = fw.link_schedules_to_contents(
        session_factory=session_factory,
        apply=True,
        use_network=False,
    )
    assert "档期条目 1 条" in summary.message
    assert "已关联 1" in summary.message


# ---------- 补库路径顺带建立关联 ----------


PAYLOAD = {
    "id": 270274,
    "type": 2,
    "name": "ネコぱら",
    "name_cn": "猫娘乐园",
    "summary": "测试用简介",
    "rating": {"rank": 0, "score": 6.5, "total": 100},
    "tags": [{"name": "萌", "count": 10}],
    "infobox": [],
    "images": {},
}

CANDIDATE = {
    "id": 270274,
    "name": "ネコぱら",
    "name_cn": "猫娘乐园",
    "date": "2026-10-05",
    "type": 2,
}


def _fake_searcher(results):
    def _search(keyword, **kwargs):
        return list(results)

    return _search


def _fake_fetcher(payload):
    def _fetch(subject_id, **kwargs):
        return dict(payload)

    return _fetch


def test_fill_records_subject_id_and_links_existing_work(session_factory):
    """补库时：已在库的作品 → 记 subject_id 并建立关联。"""
    add_content(session_factory, title="猫娘乐园", bangumi_id=270274)
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202610",
        apply=True,
        sleep=lambda _s: None,
        searcher=_fake_searcher([CANDIDATE]),
        fetcher=_fake_fetcher(PAYLOAD),
    )
    assert summary.already == 1

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.bangumi_subject_id == 270274
        assert row.content_id is not None
    finally:
        db.close()


def test_fill_records_subject_id_for_newly_imported_work(session_factory):
    """补库时：新入库的作品 → 档期行记下 subject_id 并关联。"""
    add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")

    summary = fw.fill_works_for_season(
        session_factory=session_factory,
        season_code="202610",
        apply=True,
        sleep=lambda _s: None,
        searcher=_fake_searcher([CANDIDATE]),
        fetcher=_fake_fetcher(PAYLOAD),
    )
    assert summary.imported == 1

    db = session_factory()
    try:
        row = db.query(AnimeSchedule).first()
        assert row.bangumi_subject_id == 270274
        assert row.content_id is not None
    finally:
        db.close()


# ---------- 入库管线自动连通 ----------


def test_import_links_schedule_entries_by_subject_id(session_factory):
    """新作品入库后自动反查关联档期（将来收录自动连通）。"""
    entry = add_schedule(session_factory, title_main="猫娘乐园", title_jp="ネコぱら")
    db = session_factory()
    try:
        row = db.get(AnimeSchedule, entry.id)
        row.bangumi_subject_id = 270274
        db.commit()

        outcome = ib._map_and_store(db, dict(PAYLOAD), apply=True, cover_downloader=False)
        assert outcome.status == "imported"
        assert "关联档期 1 条" in outcome.reason

        db.refresh(row)
        assert row.content_id is not None
    finally:
        db.close()


def test_import_without_matching_schedule_has_no_note(session_factory):
    """没有对应档期条目时，导入结果里不出现关联说明。"""
    db = session_factory()
    try:
        outcome = ib._map_and_store(db, dict(PAYLOAD), apply=True, cover_downloader=False)
        assert outcome.status == "imported"
        assert "关联档期" not in outcome.reason
    finally:
        db.close()


# ---------- 页面跳转 ----------


@pytest.fixture()
def linked_client(tmp_path):
    """临时库：一条已关联的档期 + 一条未关联的档期，返回已登录的客户端。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'page.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    create_accounts(factory, admin=True, user=False)

    db = factory()
    try:
        content = Content(
            title="已关联作品",
            type="anime",
            summary="简介",
            rating=7.0,
            bangumi_id=4242,
        )
        db.add(content)
        db.commit()
        linked_id = content.id
        db.add(
            AnimeSchedule(
                season_code="202610",
                source_key="linked",
                title_main="已关联作品",
                title_cn="已关联作品",
                title_jp="リンク済み",
                source_url="https://yuc.wiki/202610/",
                content_id=linked_id,
                bangumi_subject_id=4242,
            )
        )
        db.add(
            AnimeSchedule(
                season_code="202610",
                source_key="unlinked",
                title_main="未关联作品",
                title_cn="未关联作品",
                title_jp="未リンク",
                source_url="https://yuc.wiki/202610/",
            )
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
    yield client, linked_id
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_schedule_page_direct_link_when_linked(linked_client):
    """档期卡片已建立关联时直跳作品详情页。"""
    client, linked_id = linked_client
    resp = client.get("/schedule")
    assert resp.status_code == 200
    assert f'href="/contents/{linked_id}"' in resp.text


def test_schedule_page_falls_back_to_search_when_unlinked(linked_client):
    """未关联的档期卡片仍按标题搜索（零回归）。"""
    client, _linked_id = linked_client
    resp = client.get("/schedule")
    assert resp.status_code == 200
    assert "/contents?q=" in resp.text


def test_schedule_api_exposes_content_id(linked_client):
    """档期 API 带上 content_id 供前端使用。"""
    client, linked_id = linked_client
    body = client.get("/api/schedule", params={"season": "202610"}).json()
    ids = {item["title_main"]: item["content_id"] for item in body["items"]}
    assert ids["已关联作品"] == linked_id
    assert ids["未关联作品"] is None
