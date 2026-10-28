"""首页海报轮播（Coverflow）服务端名单：候选 / 轮转 / 模式 / 权限。

覆盖需求验收要点：
- 候选只收当季动画新番、必须有海报（旧季 / 漫画 / 游戏 / 无封面排除）；
- 自动更新轮转推进：名单内容会换、排序规则稳定；
- 生成失败（无候选）时保留旧名单，不把首页换成空白；
- 手动名单不被定时任务覆盖；关闭自动更新后名单原样保留；
- 频率判断：每日 / 每周（固定执行时间 06:00 后）；
- 首页渲染：有名单用名单，无名单回落当季实时候选；
- 管理页权限：游客跳登录、普通用户 403、staff 可达。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, home_posters, poster_admin
from app.database import Base, get_db
from app.main import app
from app.models import AnimeSchedule, Content, HomePoster, HomePosterConfig
from auth_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    USER_USERNAME,
    create_accounts,
    login,
)

SEASON = "202610"
OLD_SEASON = "202607"


@pytest.fixture
def poster_env(tmp_path):
    """独立临时库：一个当前季 + 一个旧季的档期，若干作品。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'posters.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    home_posters.ensure_config(factory())

    db = factory()
    try:
        # 作品库：两部当季动画（有封面）、一部漫画（应排除）、一部无封面动画（应排除）
        for title, type_, rating, image in (
            ("当季甲", "anime", 9.0, "/covers/jia.jpg"),
            ("当季乙", "anime", 8.0, "/covers/yi.jpg"),
            ("漫画丙", "comic", 9.5, "/covers/bing.jpg"),
            ("无封面丁", "anime", 8.5, None),
        ):
            db.add(Content(title=title, type=type_, summary="s", rating=rating, image_url=image))
        db.commit()
        works = {w.title: w for w in db.query(Content).all()}

        def add_schedule(**kwargs):
            row = AnimeSchedule(
                season_code=SEASON, source_url="https://example.com/x", **kwargs
            )
            db.add(row)
            return row

        add_schedule(source_key="a", title_main="当季甲", content_id=works["当季甲"].id,
                     image_url="https://img.example/a.jpg", weekday=1)
        add_schedule(source_key="b", title_main="当季乙", content_id=works["当季乙"].id,
                     image_url="https://img.example/b.jpg", weekday=2)
        add_schedule(source_key="c", title_main="漫画丙", content_id=works["漫画丙"].id,
                     image_url="https://img.example/c.jpg", weekday=3)
        add_schedule(source_key="d", title_main="无封面丁", content_id=works["无封面丁"].id,
                     image_url="https://img.example/d.jpg", weekday=4)
        # 旧季动画：不进候选
        db.add(AnimeSchedule(season_code=OLD_SEASON, source_key="old", title_main="旧作",
                             image_url="https://img.example/old.jpg",
                             source_url="https://example.com/x"))
        db.commit()
    finally:
        db.close()
    yield factory
    engine.dispose()


# ---------- 候选筛选 ----------


def test_candidates_only_current_season_anime_with_poster(poster_env):
    db = poster_env()
    try:
        titles = [c["title"] for c in home_posters.list_candidates(db)]
        assert titles == ["当季甲", "当季乙"]  # 评分降序；漫画 / 无封面 / 旧季排除
        assert all(c["href"].startswith("/contents/") for c in home_posters.list_candidates(db))
    finally:
        db.close()


def test_candidates_follow_season_switch(poster_env):
    """旧季数据升级为最新季时，候选范围跟随（季度切换即换候选）。"""
    db = poster_env()
    try:
        db.query(AnimeSchedule).update({"season_code": "202610"})
        db.commit()
        titles = [c["title"] for c in home_posters.list_candidates(db)]
        assert "旧作" in titles
    finally:
        db.close()


# ---------- 自动更新：轮转 / 生效 / 失败保留 ----------


def test_auto_update_rotates_and_persists(poster_env):
    db = poster_env()
    try:
        config = home_posters.get_config(db)
        config.mode = "auto"
        db.commit()
        first = home_posters.run_scheduled(db, force=True)
        assert first[0] is True
        names_1 = [p["title"] for p in home_posters.list_active_posters(db)]
        assert names_1 == ["当季甲", "当季乙"]
        assert home_posters.get_config(db).last_status == "ok"
        assert home_posters.get_config(db).last_run_at is not None

        # 再跑一次：候选只有 2 部（不足一屏），逐格挪位，名单内容整体换序
        #（不是反复生成同一份名单）
        home_posters.run_scheduled(db, force=True)
        names_2 = [p["title"] for p in home_posters.list_active_posters(db)]
        assert names_2 == ["当季乙", "当季甲"]
        assert home_posters.get_config(db).rotation_cursor == 0  # (1+1)%2：本次从游标 1 起取
        # 第三次再换回来（循环衔接，不会洗牌式乱序）
        home_posters.run_scheduled(db, force=True)
        names_3 = [p["title"] for p in home_posters.list_active_posters(db)]
        assert names_3 == ["当季甲", "当季乙"]
    finally:
        db.close()


def test_failed_update_keeps_previous_list(poster_env):
    """候选清空（数据异常）时更新失败：旧名单原样保留，首页不空白。"""
    db = poster_env()
    try:
        config = home_posters.get_config(db)
        config.mode = "auto"
        db.commit()
        assert home_posters.run_scheduled(db, force=True)[0] is True
        before = [p["content_id"] for p in home_posters.list_active_posters(db)]

        db.query(AnimeSchedule).delete()  # 模拟数据异常：候选归零
        db.commit()
        ok, message = home_posters.run_scheduled(db, force=True)
        assert ok is False
        assert "候选" in message
        after = [p["content_id"] for p in home_posters.list_active_posters(db)]
        assert after == before
        assert home_posters.get_config(db).last_status == "failed"
        assert home_posters.get_config(db).last_error
    finally:
        db.close()


def test_config_survives_restart(poster_env):
    """重启（重开会话再读库）后模式 / 频率 / 名单仍在。"""
    db = poster_env()
    try:
        home_posters.set_mode(db, mode="auto", frequency="weekly")
        home_posters.run_scheduled(db, force=True)
    finally:
        db.close()
    db = poster_env()  # 模拟重启后的新会话
    try:
        config = home_posters.get_config(db)
        assert config.mode == "auto"
        assert config.frequency == "weekly"
        assert len(home_posters.list_active_posters(db)) == 2
    finally:
        db.close()


# ---------- 模式与频率 ----------


def test_off_mode_never_runs(poster_env):
    db = poster_env()
    try:
        config = home_posters.get_config(db)
        assert config.mode == "off"  # 默认关闭
        # 到点（周二早上）也不跑：关闭自动更新 = 定时任务完全不动
        assert home_posters.should_run(config, now=datetime(2026, 10, 27, 7, 0)) is False
        assert home_posters.run_scheduled(db)[0] is False
        assert home_posters.list_active_posters(db) == []
    finally:
        db.close()


def test_should_run_daily(poster_env):
    db = poster_env()
    try:
        config = home_posters.get_config(db)
        config.mode = "auto"
        config.frequency = "daily"
        config.last_run_at = datetime(2026, 10, 21, 6, 30)
        db.commit()
        assert home_posters.should_run(config, now=datetime(2026, 10, 22, 5, 0)) is False
        assert home_posters.should_run(config, now=datetime(2026, 10, 22, 6, 30)) is True
        config.last_run_at = datetime(2026, 10, 22, 6, 30)
        db.commit()
        assert home_posters.should_run(config, now=datetime(2026, 10, 22, 8, 0)) is False
        assert home_posters.should_run(config, now=datetime(2026, 10, 23, 7, 0)) is True
    finally:
        db.close()


def test_should_run_weekly(poster_env):
    db = poster_env()
    try:
        config = home_posters.get_config(db)
        config.mode = "auto"
        config.frequency = "weekly"
        config.last_run_at = datetime(2026, 10, 19, 6, 30)  # 周一
        db.commit()
        assert home_posters.should_run(config, now=datetime(2026, 10, 26, 7, 0)) is True
        assert home_posters.should_run(config, now=datetime(2026, 10, 27, 7, 0)) is False
        config.last_run_at = datetime(2026, 10, 26, 6, 30)
        db.commit()
        assert home_posters.should_run(config, now=datetime(2026, 10, 29, 7, 0)) is False
        assert home_posters.should_run(config, now=datetime(2026, 11, 2, 7, 0)) is True
    finally:
        db.close()


def test_manual_list_not_overwritten_by_schedule(poster_env):
    db = poster_env()
    try:
        works = {w.title: w for w in db.query(Content).all()}
        home_posters.set_manual_list(db, [works["当季乙"].id, works["当季甲"].id])
        assert home_posters.get_config(db).mode == "manual"
        before = [p["title"] for p in home_posters.list_active_posters(db)]
        assert before == ["当季乙", "当季甲"]  # 按管理员给定顺序

        # 定时任务到点也不跑（manual 模式）
        config = home_posters.get_config(db)
        assert home_posters.should_run(config, now=datetime(2026, 10, 27, 7, 0)) is False
        # 手动「立即执行」入口只出现在非 manual 模式；即便被调用也不改 manual 名单
        # （run-now 按钮在 manual 模式下不渲染，见 admin_posters.html）
        after = [p["title"] for p in home_posters.list_active_posters(db)]
        assert after == before
    finally:
        db.close()


def test_manual_list_rejects_invalid_and_coverless(poster_env):
    db = poster_env()
    try:
        works = {w.title: w for w in db.query(Content).all()}
        count = home_posters.set_manual_list(
            db, [works["无封面丁"].id, 99999, works["当季甲"].id]
        )
        assert count == 1  # 无封面 / 不存在作品被剔除
    finally:
        db.close()


# ---------- 首页渲染与权限 ----------


def _index_client(factory):
    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_index_falls_back_to_live_candidates(poster_env):
    """无生效名单时首页回落当季实时候选（不落库、不改配置）。"""
    client = _index_client(poster_env)
    try:
        html = client.get("/").text
        assert "coverflow" in html
        assert "当季甲" in html and "当季乙" in html
        assert "/contents/" in html
        db = poster_env()
        try:
            assert home_posters.list_active_posters(db) == []  # 回落不写名单
            assert home_posters.get_config(db).mode == "off"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_index_uses_active_list(poster_env):
    db = poster_env()
    try:
        works = {w.title: w for w in db.query(Content).all()}
        home_posters.set_manual_list(db, [works["当季乙"].id])
    finally:
        db.close()
    client = _index_client(poster_env)
    try:
        html = client.get("/").text
        assert "当季乙" in html
        assert "当季甲" not in html.split("coverflow-track")[1].split("</ul>")[0]  # 名单外作品不进轮播
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_posters_admin_requires_staff(poster_env):
    """管理页权限：游客跳登录、普通用户 403、管理员可达。"""
    factory = poster_env
    create_accounts(factory)

    guest = _index_client(factory)
    try:
        resp = guest.get("/manage/posters", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/login" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.pop(get_db, None)

    user_client = _index_client(factory)
    try:
        login(user_client, USER_USERNAME, USER_PASSWORD)
        assert user_client.get("/manage/posters").status_code == 403
        # 普通用户连写操作也进不去
        assert user_client.post("/manage/posters/mode", data={"mode": "auto"}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)

    admin_client = _index_client(factory)
    try:
        login(admin_client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = admin_client.get("/manage/posters")
        assert resp.status_code == 200
        assert "更新模式" in resp.text and "手动指定" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_admin_can_switch_mode_and_run(poster_env):
    """管理员切到自动更新：立即执行一次，名单生效；再切关闭：名单保留。"""
    factory = poster_env
    create_accounts(factory)
    client = _index_client(factory)
    try:
        login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = client.post(
            "/manage/posters/mode", data={"mode": "auto", "frequency": "daily"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = factory()
        try:
            config = home_posters.get_config(db)
            assert config.mode == "auto"
            assert config.frequency == "daily"
            assert config.last_status == "ok"
            assert len(home_posters.list_active_posters(db)) == 2
        finally:
            db.close()
        # 关闭自动更新：名单与排序原样保留
        client.post("/manage/posters/mode", data={"mode": "off"})
        db = factory()
        try:
            assert home_posters.get_config(db).mode == "off"
            titles = [p["title"] for p in home_posters.list_active_posters(db)]
            assert titles == ["当季甲", "当季乙"]
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------- 管理页：全站候选池 / 搜索 / 加入 / 移除 ----------


def test_manual_candidates_cover_whole_site(poster_env):
    """手动候选池是全站带封面作品（不再限当季新番）：漫画也在内，无封面排除。"""
    factory = poster_env
    db = factory()
    try:
        total, items, pages = poster_admin.search_manual_candidates(db)
        titles = {c["title"] for c in items}
        assert total == 3
        assert titles == {"当季甲", "当季乙", "漫画丙"}
        assert all(c["href"] == f"/contents/{c['content_id']}" for c in items)
        assert pages == 1
    finally:
        db.close()


def test_manual_candidates_search_and_filter(poster_env):
    """搜索口径：标题模糊 / 纯数字按 ID / 类型筛选；非法类型被忽略。"""
    factory = poster_env
    db = factory()
    try:
        jia_id = db.query(Content).filter_by(title="当季甲").one().id
        total, items, _ = poster_admin.search_manual_candidates(db, keyword="当季")
        assert total == 2
        total, items, _ = poster_admin.search_manual_candidates(db, keyword=str(jia_id))
        assert total == 1 and items[0]["title"] == "当季甲"
        total, items, _ = poster_admin.search_manual_candidates(
            db, content_type="comic"
        )
        assert total == 1 and items[0]["title"] == "漫画丙"
        # 未知类型值：不当筛选条件，退回全量
        total, _, _ = poster_admin.search_manual_candidates(db, content_type="nope")
        assert total == 3
        # 无命中
        total, items, _ = poster_admin.search_manual_candidates(
            db, keyword="不存在的作品"
        )
        assert total == 0 and items == []
    finally:
        db.close()


def test_admin_add_remove_manual_posters(poster_env):
    """加入累加（翻页上下文保留）、移除、模式切手动；空勾选不破坏名单。"""
    factory = poster_env
    create_accounts(factory)
    client = _index_client(factory)
    try:
        login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        db = factory()
        try:
            jia_id = db.query(Content).filter_by(title="当季甲").one().id
            bing_id = db.query(Content).filter_by(title="漫画丙").one().id
        finally:
            db.close()
        # 加入当季甲：重定向保留搜索 / 翻页上下文
        resp = client.post(
            "/manage/posters/add",
            data={"content_ids": str(jia_id), "q": "当季", "type": "anime", "page": "2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "q=" in location and "page=2" in location
        db = factory()
        try:
            config = home_posters.get_config(db)
            assert config.mode == "manual"
            assert [p["content_id"] for p in home_posters.list_active_posters(db)] == [jia_id]
        finally:
            db.close()
        # 再加入漫画丙：累加，不清空之前的
        client.post("/manage/posters/add", data={"content_ids": str(bing_id)})
        db = factory()
        try:
            assert [p["content_id"] for p in home_posters.list_active_posters(db)] == [
                jia_id,
                bing_id,
            ]
        finally:
            db.close()
        # 空勾选：报错且不动名单
        resp = client.post(
            "/manage/posters/add", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        db = factory()
        try:
            assert len(home_posters.list_active_posters(db)) == 2
        finally:
            db.close()
        # 移除当季甲（移除与保存排序同一端点，靠 remove_id 区分）
        resp = client.post(
            "/manage/posters/manual",
            data={
                "content_ids": [str(jia_id), str(bing_id)],
                f"order_{jia_id}": "1",
                f"order_{bing_id}": "2",
                "remove_id": str(jia_id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = factory()
        try:
            assert [p["content_id"] for p in home_posters.list_active_posters(db)] == [bing_id]
        finally:
            db.close()
        # 移除最后一张 = 清空名单（首页回落当季实时候选）
        resp = client.post(
            "/manage/posters/manual",
            data={"content_ids": str(bing_id), "remove_id": str(bing_id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "notice=" in resp.headers["location"]
        db = factory()
        try:
            assert home_posters.list_active_posters(db) == []
            assert home_posters.get_config(db).mode == "manual"
        finally:
            db.close()
        # 再加入当季甲并保存排序（保持 manual 模式）
        client.post("/manage/posters/add", data={"content_ids": str(jia_id)})
        client.post(
            "/manage/posters/manual",
            data={"content_ids": str(jia_id), f"order_{jia_id}": "1"},
        )
        db = factory()
        try:
            config = home_posters.get_config(db)
            assert config.mode == "manual"
            assert [p["content_id"] for p in home_posters.list_active_posters(db)] == [jia_id]
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_admin_manual_page_shows_search_form(poster_env):
    """管理页带全站候选池搜索表单与名单管理入口；空名单提示回落。"""
    factory = poster_env
    create_accounts(factory)
    client = _index_client(factory)
    try:
        login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = client.get("/manage/posters")
        assert resp.status_code == 200
        assert "候选池" in resp.text
        assert "搜索作品" in resp.text
        assert "全站带封面作品" in resp.text
        assert "加入选中作品" in resp.text
        # 普通用户仍旧 403（含写操作）
        user_client = _index_client(factory)
        try:
            login(user_client, USER_USERNAME, USER_PASSWORD)
            assert user_client.post("/manage/posters/add", data={"content_ids": "1"}).status_code == 403
            assert user_client.post("/manage/posters/manual", data={"content_ids": "1"}).status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        app.dependency_overrides.pop(get_db, None)
