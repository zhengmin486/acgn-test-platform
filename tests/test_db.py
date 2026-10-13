"""数据库访问层测试：模型、CRUD 与播种幂等，用独立临时 SQLite 验证。

不依赖 MySQL，不触碰应用默认数据库文件，也不接触旧短链接项目数据库。
"""

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.crud import get_content, list_contents, search_contents, seed_if_empty
from app.database import Base, build_engine
from app.fake_data import FAKE_CONTENTS
from app.models import Content


def make_session(tmp_path, name: str):
    """每个用例一个独立临时 SQLite，互相隔离。"""
    engine = build_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_seed_and_list_roundtrip(tmp_path):
    """播种后可按顺序读出全部数据，且字段完整。"""
    db = make_session(tmp_path, "seed.db")
    assert seed_if_empty(db) == len(FAKE_CONTENTS)
    items = list_contents(db)
    assert [i.id for i in items] == [row["id"] for row in FAKE_CONTENTS]
    assert items[0].as_dict() == FAKE_CONTENTS[0]


def test_seed_idempotent(tmp_path):
    """重复初始化不写重复数据、不覆盖已有内容。"""
    db = make_session(tmp_path, "idem.db")
    assert seed_if_empty(db) == len(FAKE_CONTENTS)
    assert seed_if_empty(db) == 0
    assert len(list_contents(db)) == len(FAKE_CONTENTS)


def test_get_content_hit_and_miss(tmp_path):
    """主键查询：命中返回模型对象，未命中返回 None。"""
    db = make_session(tmp_path, "get.db")
    seed_if_empty(db)
    assert get_content(db, 3).title == "迷宫回响"
    assert get_content(db, 999) is None


def test_search_matches_title_and_summary(tmp_path):
    """访问层搜索语义与 API 契约一致：大小写不敏感、标题与简介均参与。"""
    db = make_session(tmp_path, "search.db")
    seed_if_empty(db)
    assert [c.id for c in search_contents(db, "CYBER")] == [5]
    assert [c.id for c in search_contents(db, "乐队")] == [6]
    assert search_contents(db, "绝不存在的词") == []


def test_model_write_update_delete(tmp_path):
    """超出只读 API 的部分：建、改、删在表结构上真实可用。"""
    db = make_session(tmp_path, "write.db")
    db.add(Content(id=100, title="临时条目", type="novel", summary="临时简介", rating=6.5))
    db.commit()
    row = db.get(Content, 100)
    assert row is not None
    row.rating = 7.0
    db.commit()
    assert db.get(Content, 100).rating == 7.0
    db.delete(db.get(Content, 100))
    db.commit()
    assert db.scalars(select(Content.id)).all() == []
