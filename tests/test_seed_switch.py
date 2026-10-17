"""种子数据开关测试：SEED_DEMO_DATA 的两种状态。

覆盖：
- 开关打开（默认）：init_db 首次播种 FAKE_CONTENTS，且幂等（已有数据不重播）；
- 开关关闭：contents 保持干净（上线形态），但默认账号仍会创建——
  种子开关只管演示内容，不管账号体系；
- seed_if_empty 单函数行为：关闭时直接返回 0、不写任何行。

使用独立临时 SQLite（tmp_path），与开发库 local_dev.db 完全隔离。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import build_engine
from app.models import Content, User


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


class TestSeedSwitch:
    def test_enabled_seeds_demo_contents(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "seed_demo_data", True)
        engine = build_engine(f"sqlite:///{tmp_path / 'seeded.db'}")
        crud.init_db(engine)

        db = _session(engine)
        try:
            assert _count(db, Content) == len(crud.FAKE_CONTENTS)
        finally:
            db.close()
            engine.dispose()

    def test_disabled_keeps_contents_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "seed_demo_data", False)
        engine = build_engine(f"sqlite:///{tmp_path / 'clean.db'}")
        crud.init_db(engine)

        db = _session(engine)
        try:
            # contents 一张不写：上线用干净库，真实数据靠后台同步灌入
            assert _count(db, Content) == 0
            # 但账号体系不受影响：初始管理员 / 普通用户照常创建
            assert _count(db, User) >= 2
        finally:
            db.close()
            engine.dispose()

    def test_disabled_seed_if_empty_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "seed_demo_data", False)
        engine = build_engine(f"sqlite:///{tmp_path / 'no_seed.db'}")
        crud.init_db(engine)

        db = _session(engine)
        try:
            assert crud.seed_if_empty(db) == 0
            assert _count(db, Content) == 0
        finally:
            db.close()
            engine.dispose()

    def test_seeding_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "seed_demo_data", True)
        engine = build_engine(f"sqlite:///{tmp_path / 'twice.db'}")
        crud.init_db(engine)

        db = _session(engine)
        try:
            assert crud.seed_if_empty(db) == 0  # 已有数据，不再播种
            assert _count(db, Content) == len(crud.FAKE_CONTENTS)
        finally:
            db.close()
            engine.dispose()

    def test_app_starts_clean_when_switch_off(self, tmp_path, monkeypatch):
        """SEED_DEMO_DATA=false 时经 TestClient 走一遍启动流程，contents 仍为空。"""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.main import app

        monkeypatch.setattr(settings, "seed_demo_data", False)
        engine = build_engine(f"sqlite:///{tmp_path / 'app_clean.db'}")
        crud.init_db(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(app) as client:
                resp = client.get("/")
                assert resp.status_code == 200
            db = session_factory()
            try:
                assert _count(db, Content) == 0
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
