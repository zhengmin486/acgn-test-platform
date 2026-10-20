"""书籍细分标注「文学」的页面角标测试。

背景（2026-10-19）：轻小说区混进了日本普通小说（一度还包括并不属于本站的非日本
语种书籍）。站的口径是——**文学不是第五种类型，只是 novel 上的一枚角标**：

    - 类型始终是 novel，不新增类型枚举、不改筛选与导航；
    - ``contents.book_kind`` 取值 light_novel / literature，literature 时页面多一枚
      辅助色的「文学」角标，让用户一眼看出「这不是文库出身的轻小说」。

这里验证的是展示层：列表卡片与详情页都要有角标，且只有 literature 才有。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Content

LITERATURE_TITLE = "人间失格"
LIGHT_NOVEL_TITLE = "とある魔術の禁書目録"
ANIME_TITLE = "对照动画"
KIND_BADGE_CLASS = "detail-kind-badge"


class BookFixture:
    """临时库上的书籍测试数据 + 客户端。"""

    def __init__(self, client, session_factory):
        self.client = client
        self.session_factory = session_factory

    def content_id(self, title: str) -> int:
        with self.session_factory() as db:
            return int(db.scalar(select(Content.id).where(Content.title == title)))


@pytest.fixture
def books(tmp_path):
    """一条 literature 小说 + 一条 light_novel 小说 + 一条动画作对照。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bookkind.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            Content(
                title=LITERATURE_TITLE,
                type="novel",
                summary="太宰治的中篇小说。",
                rating=8.6,
                book_kind="literature",
            )
        )
        db.add(
            Content(
                title=LIGHT_NOVEL_TITLE,
                type="novel",
                summary="鎌池和馬的轻小说。",
                rating=7.6,
                book_kind="light_novel",
            )
        )
        db.add(Content(title=ANIME_TITLE, type="anime", summary="不是书籍。", rating=7.0))
        db.commit()

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    yield BookFixture(TestClient(app), factory)
    app.dependency_overrides.pop(get_db, None)


def _cards(html: str) -> dict[str, str]:
    """把列表页卡片按标题切成块，返回 {标题: 整卡 HTML}。

    标题模式随视图演进而增加：work-card-title（网格）/ bili-title（B站卡片，
    2026-10-20 起的默认视图）/ fw-semibold text-truncate（列表行）。
    块取整卡（而不是标题之后的片段）：B站卡片的类型签与「文学」角标在封面区、
    位于标题之前，按标题后切片会漏掉它们。
    """
    blocks: dict[str, str] = {}
    for chunk in html.split('href="/contents/')[1:]:
        ident = chunk.split('"', 1)[0]
        if not ident.strip().isdigit():
            continue
        title = None
        for pattern in ('work-card-title">', 'bili-title">', 'fw-semibold text-truncate">'):
            if pattern in chunk:
                title = chunk.split(pattern, 1)[1].split("<", 1)[0].strip()
                break
        if title:
            blocks[title] = chunk
    return blocks


class TestLiteratureBadge:
    def test_listing_marks_only_literature(self, books):
        response = books.client.get("/contents?type=novel")
        assert response.status_code == 200
        cards = _cards(response.text)
        assert LITERATURE_TITLE in cards, "文学条目应出现在轻小说区（它仍是 novel）"
        assert LIGHT_NOVEL_TITLE in cards
        assert KIND_BADGE_CLASS in cards[LITERATURE_TITLE]
        assert KIND_BADGE_CLASS not in cards[LIGHT_NOVEL_TITLE]

    def test_literature_card_keeps_novel_type_label(self, books):
        """角标是**额外**信息：类型标签仍然写着「轻小说」。"""
        cards = _cards(books.client.get("/contents?type=novel").text)
        assert "轻小说" in cards[LITERATURE_TITLE]

    def test_detail_page_marks_literature(self, books):
        response = books.client.get(f"/contents/{books.content_id(LITERATURE_TITLE)}")
        assert response.status_code == 200
        assert KIND_BADGE_CLASS in response.text
        assert "文学" in response.text

    def test_light_novel_detail_has_no_kind_badge(self, books):
        ident = books.content_id(LIGHT_NOVEL_TITLE)
        assert KIND_BADGE_CLASS not in books.client.get(f"/contents/{ident}").text

    def test_non_book_entries_have_no_badge(self, books):
        """动画等书籍以外的内容不该被误挂书籍细分角标。"""
        response = books.client.get("/contents?type=anime")
        assert response.status_code == 200
        cards = _cards(response.text)
        assert ANIME_TITLE in cards
        for block in cards.values():
            assert KIND_BADGE_CLASS not in block


class TestBookKindColumn:
    """历史库幂等补列：没有 Alembic，book_kind 只能靠 ensure 函数补上来。"""

    @pytest.fixture
    def legacy_engine(self, tmp_path) -> Engine:
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE contents ("
                    "id INTEGER PRIMARY KEY, title VARCHAR(200), type VARCHAR(20),"
                    " summary TEXT, rating FLOAT)"
                )
            )
        yield engine
        engine.dispose()

    def test_column_is_added_once(self, legacy_engine):
        assert crud.ensure_content_book_kind_column(legacy_engine) == ["book_kind"]
        columns = {column["name"] for column in inspect(legacy_engine).get_columns("contents")}
        assert "book_kind" in columns
        assert crud.ensure_content_book_kind_column(legacy_engine) == []

    def test_missing_table_is_noop(self, legacy_engine):
        with legacy_engine.begin() as connection:
            connection.execute(text("DROP TABLE contents"))
        assert crud.ensure_content_book_kind_column(legacy_engine) == []
