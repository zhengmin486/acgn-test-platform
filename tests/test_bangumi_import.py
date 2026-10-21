"""Bangumi 导入功能测试。

原则（与项目测试风格一致）：
- 外部 API 全部用假 opener / stub fetch 模拟，不访问真实网络；
- 数据库写入使用独立临时 SQLite（tmp_path），不触碰 local_dev.db；
- 不执行正式导入 --apply 的真实调用，仅在解析层面验证参数。
"""

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import crud
from app.crud import seed_if_empty
from app.database import Base, build_engine, get_db
from app.fake_data import FAKE_CONTENTS
from app.main import app
from app.models import Content, ContentTag, Tag

# scripts/ 不是包，这里按文件路径加载被测模块（与直接运行脚本等价）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "import_bangumi.py"

_spec = importlib.util.spec_from_file_location("import_bangumi_for_tests", SCRIPT_PATH)
import_bangumi = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = import_bangumi  # dataclass 装饰器依赖模块已注册到 sys.modules
_spec.loader.exec_module(import_bangumi)

# ---------- 测试数据 ----------

ANIME_PAYLOAD = {
    "id": 265,
    "type": 2,
    "name": "聖闘士星矢 セイントセイヤ ギャラクシーウォー編",
    "name_cn": "圣斗士星矢",
    "summary": "少年们为了守护雅典娜而战的热血故事。",
    "rating": {"rank": 23, "total": 34602, "score": 8.7},
    # 8 个标签：只应导入标注数最多的前 5 个
    "tags": [
        {"name": "热血", "count": 500},
        {"name": "科幻", "count": 346},
        {"name": "机甲", "count": 120},
        {"name": "战斗", "count": 98},
        {"name": "友情", "count": 76},
        {"name": "冷门标签", "count": 12},
        {"name": "更冷门", "count": 3},
        {"name": "几乎没人标", "count": 1},
    ],
}

GAME_PAYLOAD = {
    "id": 975,
    "type": 4,
    "name": "Undertale",
    "summary": "一款不杀死任何怪物也可以通关的 RPG。",
    "rating": {"score": 9.4},
    # 2026-10-21 收录口径：非 galge 游戏需选品端查过镜像关联、确认有动画/漫画
    # 相关并打标（scripts.select_from_archive.apply_ingest_policy），本夹具模拟
    # 「已确认相关」的形态。
    "_site_related_ip": True,
}


def make_payload(bangumi_id: int, bgm_type: int) -> dict:
    """快速构造一个带基本字段的条目 payload。"""
    return {
        "id": bangumi_id,
        "type": bgm_type,
        "name": f"entry-{bangumi_id}",
        "name_cn": f"条目{bangumi_id}",
        "summary": f"条目 {bangumi_id} 的简介。",
        "rating": {"score": 7.5},
    }


class FakeResponse:
    """模拟 opener.open 返回的响应体（上下文管理器）。"""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """按脚本逐次返回预设响应或抛出异常的假 opener，并记录每次请求。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def open(self, request, timeout=None):
        self.calls.append(
            {
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "timeout": timeout,
            }
        )
        if not self._script:
            raise AssertionError("脚本已耗尽，收到了多余的请求")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, (bytes, bytearray)):
            return FakeResponse(bytes(step))
        return FakeResponse(json.dumps(step).encode("utf-8"))


def make_stub_fetch(payloads: dict[int, dict]):
    """用字典实现的假 fetch(subject_id)：命中返回 payload，未命中直接报错。"""
    calls: list[int] = []

    def fetch(subject_id: int) -> dict:
        calls.append(subject_id)
        if subject_id not in payloads:
            raise import_bangumi.BangumiAPIError(f"获取条目 {subject_id} 失败：模拟网络故障")
        return payloads[subject_id]

    fetch.calls = calls
    return fetch


def make_db(tmp_path, name: str = "bangumi.db"):
    """每个用例一个独立临时 SQLite，返回 (engine, session工厂)；已建全部表。"""
    engine = build_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def seeded_db(tmp_path, name: str = "bangumi.db"):
    """建库并播种 6 条示例数据，返回 (engine, session工厂)。"""
    engine, factory = make_db(tmp_path, name)
    db = factory()
    try:
        assert seed_if_empty(db) == len(FAKE_CONTENTS)
    finally:
        db.close()
    return engine, factory


def all_contents(factory) -> list[Content]:
    db = factory()
    try:
        return list(db.query(Content).order_by(Content.id))
    finally:
        db.close()


# ---------- 映射规则：API JSON -> Content 字段 ----------


class TestMapSubject:
    def test_anime_full_mapping(self):
        """type=2 映射 anime；标题取 name_cn；summary/rating/source_url 各归其位。"""
        subject = import_bangumi.map_subject(ANIME_PAYLOAD)

        assert subject.bangumi_id == 265
        assert subject.title == "圣斗士星矢"
        assert subject.type == "anime"
        assert subject.summary == "少年们为了守护雅典娜而战的热血故事。"
        assert subject.rating == pytest.approx(8.7)
        assert subject.source_url == "https://bgm.tv/subject/265"

    def test_game_mapping_with_name_fallback(self):
        """type=4 映射 game；name_cn 缺失时标题回退到 name。"""
        subject = import_bangumi.map_subject(GAME_PAYLOAD)

        assert subject.type == "game"
        assert subject.title == "Undertale"
        assert subject.bangumi_id == 975
        assert subject.source_url == "https://bgm.tv/subject/975"

    @pytest.mark.parametrize(
        ("bgm_type", "platform", "expected_type"),
        [
            (2, "TV", "anime"),
            (4, "PC", "game"),
            (1, 1001, "comic"),   # 归档：书籍区 1001=漫画
            # (1, 1002, ...) 无文库信号的小说 = 文学，2026-10-21 起不收（见
            # TestIngestPolicy20261021）；类型解析本身仍见 test_book_classify。
            (1, 1002, None),
            (1, "漫画", "comic"),  # API 字符串 platform
            (1, "轻小说", "novel"),
            (1, 1003, None),      # 画集不收
            (1, None, None),      # 书籍区解析不出细分也不收
        ],
    )
    def test_site_types_resolution(self, bgm_type, platform, expected_type):
        """站点四类型的解析规则（2026-10-18 按官方归档实测语义修正）：

        type=2→anime、4→game；type=1 书籍按 platform 拆分（1001=漫画、1002=小说）；
        type=6 是三次元真人剧（Archive README 写「6=漫画」是错的），不收录。
        2026-10-21 收录口径：游戏须 galge 或带相关 IP 标记，小说须轻小说。
        """
        payload = make_payload(400, bgm_type)
        payload["platform"] = platform
        if bgm_type == 4:
            # 游戏走 galge 通道（tags 判定）；解析规则本身与本口径正交
            payload["tags"] = [{"name": "GALGAME", "count": 10}]

        if expected_type is None:
            with pytest.raises(import_bangumi.SubjectMappingError):
                import_bangumi.map_subject(payload)
        else:
            subject = import_bangumi.map_subject(payload)
            assert subject.type == expected_type

    @pytest.mark.parametrize(
        ("bgm_type", "expected_reason"),
        [(3, "音乐"), (6, "真人"), (None, "未知类型")],
    )
    def test_unsupported_types_are_rejected(self, bgm_type, expected_reason):
        """音乐 / 真人（三次元）/ 缺失类型一律跳过（不属于本站四类型）。"""
        payload = make_payload(300, 1)
        payload["type"] = bgm_type

        with pytest.raises(import_bangumi.SubjectMappingError) as excinfo:
            import_bangumi.map_subject(payload)

        assert expected_reason in str(excinfo.value)

    def test_missing_rating_is_rejected(self):
        """rating.score 缺失或为 0 时跳过，不伪造评分。"""
        for score in (None, 0):
            payload = make_payload(301, 2)
            payload["rating"] = {"score": score} if score is not None else {}

            with pytest.raises(import_bangumi.SubjectMappingError, match="评分"):
                import_bangumi.map_subject(payload)

    def test_missing_summary_is_rejected(self):
        """summary 为空时跳过（本站 summary 为必填）。"""
        payload = make_payload(302, 2)
        payload["summary"] = "   "

        with pytest.raises(import_bangumi.SubjectMappingError, match="简介"):
            import_bangumi.map_subject(payload)

    def test_missing_summary_uses_fallback(self):
        """仅可信补库路径传 summary_fallback 时，空简介用档期信息兜底、不报错。"""
        payload = make_payload(302, 2)
        payload["summary"] = "   "
        fallback = "【原创动画】热血（Bangumi 暂无官方简介，信息来自新番档期）"

        subject = import_bangumi.map_subject(payload, summary_fallback=fallback)
        assert subject.summary == fallback

    def test_empty_fallback_still_rejected(self):
        """fallback 本身是空白时，仍走严格必填、不伪造。"""
        payload = make_payload(302, 2)
        payload["summary"] = "   "

        with pytest.raises(import_bangumi.SubjectMappingError, match="简介"):
            import_bangumi.map_subject(payload, summary_fallback="   ")

    def test_missing_title_is_rejected(self):
        """name_cn 与 name 均为空时跳过。"""
        payload = make_payload(303, 2)
        payload["name"] = "  "
        payload["name_cn"] = ""

        with pytest.raises(import_bangumi.SubjectMappingError, match="标题"):
            import_bangumi.map_subject(payload)

    def test_missing_or_invalid_id_is_rejected(self):
        """缺少/非法 id 时跳过，映射不产生半成品记录。"""
        for bad_id in (None, 0, -1, "265"):
            payload = make_payload(1, 2)
            payload["id"] = bad_id

            with pytest.raises(import_bangumi.SubjectMappingError, match="id"):
                import_bangumi.map_subject(payload)

    def test_overlong_title_is_rejected(self):
        """标题超过 200 字符（title 列上限）时跳过，而不是截断或入库失败。"""
        payload = make_payload(304, 2)
        payload["name_cn"] = "长" * 201

        with pytest.raises(import_bangumi.SubjectMappingError, match="200"):
            import_bangumi.map_subject(payload)


# ---------- 标签提取：按标注数取前 N ----------


class TestExtractTags:
    def test_top_tags_by_count(self):
        """按 count 从多到少取前 5 个题材标签，标注数少的被截掉。"""
        tags = import_bangumi.extract_tags(ANIME_PAYLOAD)

        assert tags == ["热血", "科幻", "机甲", "战斗", "友情"]
        assert import_bangumi.MAX_TAGS_PER_SUBJECT == 5

    def test_map_subject_carries_tags(self):
        """map_subject 把截断后的标签带进 MappedSubject。"""
        subject = import_bangumi.map_subject(ANIME_PAYLOAD)

        assert subject.tags == ["热血", "科幻", "机甲", "战斗", "友情"]

    def test_missing_or_invalid_tags_field_returns_empty(self):
        """tags 缺失 / 空列表 / 非列表：返回空列表，不影响作品映射。"""
        for payload_tags in (None, [], "热血", {"name": "科幻"}):
            payload = make_payload(401, 2)
            payload["tags"] = payload_tags
            assert import_bangumi.extract_tags(payload) == []

        assert import_bangumi.extract_tags({}) == []

    def test_non_genre_tags_dropped_before_limit(self):
        """年份 / 公司 / 媒介 / 平台在入库前被丢掉，不占题材名额。

        这条是线上教训：不过滤时标签云被 TV / 剧场版 / PC 这类媒介词霸榜，
        「按标签筛选」点哪个都是全站作品，等于废掉。放宽白名单后，未进白名单的
        真实主题词（作画）会保留，但作品信息（年份 / 公司 / 媒介 / 平台）与已知
        噪音仍坚决排除。作者 / 声优不出现在 Bangumi 的 tags 字段（在 characters /
        persons 接口），若有则经 exclude 参数兜底排除（见下条）。
        """
        payload = make_payload(402, 2)
        payload["tags"] = [
            {"name": "TV", "count": 9999},
            {"name": "2011", "count": 5000},
            {"name": "A-1 Pictures", "count": 4000},
            {"name": "剧场版", "count": 2000},
            {"name": "PC", "count": 1000},
            {"name": "作画", "count": 500},  # 未进白名单的真实主题 -> 保留
            {"name": "治愈", "count": 100},
            {"name": "日常", "count": 90},
        ]

        # 作品信息 + 已知噪音被丢弃；真实主题（作画）保留，按标注数取前 N 个
        assert import_bangumi.extract_tags(payload) == ["作画", "治愈", "日常"]

    def test_person_names_excluded_via_exclude_param(self):
        """声优 / 主创名经 exclude 参数兜底排除，不进标签云（与「作者声优不进标签」一致）。"""
        payload = make_payload(403, 2)
        payload["tags"] = [
            {"name": "花泽香菜", "count": 3000},
            {"name": "治愈", "count": 100},
        ]
        # 导入路径（无 exclude）无法区分声优名与未知主题，但 Bangumi tags 不含人物名；
        # 回源同步路径持有真实人名，经 exclude 排除 -> 声优名不进标签。
        assert import_bangumi.extract_tags(payload, exclude={"花泽香菜"}) == ["治愈"]

    def test_genre_aliases_normalized_on_import(self):
        """同义词入库即归一：SF -> 科幻、日常系 -> 日常。"""
        payload = make_payload(405, 2)
        payload["tags"] = [
            {"name": "SF", "count": 100},
            {"name": "日常系", "count": 90},
            {"name": "癒し", "count": 80},
        ]

        assert import_bangumi.extract_tags(payload) == ["科幻", "日常", "治愈"]

    def test_invalid_entries_are_skipped(self):
        """非 dict 元素、空名、超长名一律丢弃；count 非法按 0 排在最后。"""
        payload = make_payload(406, 2)
        payload["tags"] = [
            "not-a-dict",
            {"name": "", "count": 99},
            {"name": "长" * 81, "count": 98},
            {"name": "治愈", "count": 0},
            {"name": "日常", "count": "abc"},
            {"name": "热血", "count": 1},
        ]

        # count 非法按 0：两个 0 分标签按名称 codepoint 排序（日 < 热），结果确定
        assert import_bangumi.extract_tags(payload) == ["热血", "日常", "治愈"]

    def test_duplicate_names_deduped_keep_first(self):
        """同名标签去重（保留先出现的那次），不占两个名额。"""
        payload = make_payload(403, 2)
        payload["tags"] = [
            {"name": "科幻", "count": 10},
            {"name": "机甲", "count": 9},
            {"name": "科幻", "count": 8},
        ]

        assert import_bangumi.extract_tags(payload) == ["科幻", "机甲"]

    def test_same_count_sorted_by_name_for_stability(self):
        """标注数相同按名称排序，保证导入结果确定可复现。"""
        payload = make_payload(404, 2)
        payload["tags"] = [
            {"name": "治愈", "count": 5},
            {"name": "热血", "count": 5},
            {"name": "机甲", "count": 5},
        ]

        assert import_bangumi.extract_tags(payload) == ["机甲", "治愈", "热血"]

    def test_custom_limit(self):
        """limit 参数可显式指定（与 crud.MAX_CONTENT_TAGS 兜底一致）。"""
        assert import_bangumi.extract_tags(ANIME_PAYLOAD, limit=2) == ["热血", "科幻"]
        assert import_bangumi.extract_tags(ANIME_PAYLOAD, limit=0) == []


class TestExtractStudiosAndYear:
    def test_studios_from_tags_are_normalized(self):
        """标签里的公司归一后进 studios，不占题材名额。"""
        payload = make_payload(410, 2)
        payload["tags"] = [
            {"name": "京阿尼", "count": 100},
            {"name": "A1 Pictures", "count": 90},
            {"name": "治愈", "count": 80},
            {"name": "TV", "count": 9999},
        ]

        assert import_bangumi.extract_studios(payload) == ["京都动画", "A-1 Pictures"]
        # 公司不影响题材标签
        assert import_bangumi.extract_tags(payload) == ["治愈"]

    def test_studios_capped(self):
        payload = make_payload(411, 2)
        payload["tags"] = [
            {"name": "BONES", "count": 10},
            {"name": "MAPPA", "count": 9},
            {"name": "SHAFT", "count": 8},
            {"name": "TRIGGER", "count": 7},
        ]

        assert import_bangumi.extract_studios(payload, limit=2) == ["BONES", "MAPPA"]

    def test_year_prefers_date_then_tags(self):
        payload = make_payload(412, 2)
        payload["date"] = "2006-04-02"
        payload["tags"] = [{"name": "2011", "count": 5}]
        assert import_bangumi.extract_year(payload) == 2006

        payload["date"] = ""
        assert import_bangumi.extract_year(payload) == 2011  # 标签年份兜底

        payload["tags"] = [{"name": "治愈", "count": 5}]
        assert import_bangumi.extract_year(payload) is None

    def test_map_subject_carries_studios_and_year(self):
        payload = make_payload(413, 2)
        payload["date"] = "2015-10-01"
        payload["tags"] = [
            {"name": "BONES", "count": 10},
            {"name": "热血", "count": 9},
            {"name": "2015", "count": 1},
        ]

        subject = import_bangumi.map_subject(payload)
        assert subject.studios == ["BONES"]
        assert subject.year == 2015


class TestExtractBookTags:
    def test_bunko_label_becomes_tag(self):
        """轻小说的文库标签补进小说区筛选（infobox 派生，不过归一）。"""
        payload = make_payload(420, 1)
        payload["platform"] = 1002
        payload["infobox"] = [
            {"key": "作者", "value": "镰池和马"},
            {"key": "文库", "value": "電撃文庫"},
            {"key": "出版社", "value": "KADOKAWA"},
        ]

        assert import_bangumi.extract_book_tags(payload) == ["電撃文庫"]

    def test_japanese_and_chinese_keys(self):
        payload = make_payload(421, 1)
        payload["infobox"] = [
            {"key": "レーベル", "value": "角川スニーカー文庫"},
            {"key": "丛书", "value": ["轻小说", "日本文学"]},
        ]

        assert import_bangumi.extract_book_tags(payload) == ["角川スニーカー文庫", "轻小说", "日本文学"]

    def test_other_keys_ignored_and_capped(self):
        payload = make_payload(422, 1)
        payload["infobox"] = [
            {"key": "出版社", "value": "讲谈社"},
            {"key": "文库", "value": "A"},
            {"key": "文庫", "value": "B"},
            {"key": "label", "value": "C"},
            {"key": "imprint", "value": "D"},
        ]

        # 出版社由 entity_links 收进关联实体，不在这里派生；上限 3 个
        assert import_bangumi.extract_book_tags(payload) == ["A", "B", "C"]

    def test_missing_infobox_returns_empty(self):
        payload = make_payload(423, 1)
        assert import_bangumi.extract_book_tags(payload) == []
        payload["infobox"] = "not-a-list"
        assert import_bangumi.extract_book_tags(payload) == []

    def test_novel_mapping_appends_bunko_tag(self):
        """书籍条目：归一题材 + infobox 文库标签一起进 MappedSubject。"""
        payload = make_payload(424, 1)
        payload["platform"] = 1002
        payload["infobox"] = [{"key": "文库", "value": "電撃文庫"}]
        payload["tags"] = [
            {"name": "轻小说", "count": 500},
            {"name": "科幻", "count": 100},
            {"name": "魔法", "count": 90},
        ]

        subject = import_bangumi.map_subject(payload)
        assert subject.type == "novel"
        assert subject.book_kind == "light_novel"
        # 「轻小说」是载体词（归一后属噪音），文库标签补位
        assert subject.tags == ["科幻", "魔法", "電撃文庫"]


class TestIngestPolicy20261021:
    """收录口径（2026-10-21 用户定稿）：小说只收轻小说，游戏只收 galge / 相关 IP。

    - classify_book 的语义不变（文学仍返回 ("novel", "literature")，存量打标
      scripts/mark_book_kinds.py 依赖它），拦人在 map_subject 的入库闸口；
    - 游戏的 galge 判定只读 tags（is_galge_payload，不出网）；「有动画/漫画
      相关」由选品端查镜像后打 payload["_site_related_ip"]=True。
    """

    @staticmethod
    def _novel_payload() -> dict:
        payload = make_payload(510, 1)
        payload["platform"] = 1002
        payload["infobox"] = [{"key": "出版社", "value": "新潮社"}]
        return payload

    @staticmethod
    def _game_payload(**extra) -> dict:
        payload = make_payload(520, 4)
        payload.update(extra)
        return payload

    def test_literature_novel_rejected(self):
        with pytest.raises(import_bangumi.SubjectMappingError) as excinfo:
            import_bangumi.map_subject(self._novel_payload())
        assert "文学" in str(excinfo.value)

    def test_light_novel_still_imports(self):
        payload = self._novel_payload()
        payload["tags"] = [{"name": "轻小说", "count": 300}]
        subject = import_bangumi.map_subject(payload)
        assert subject.type == "novel"
        assert subject.book_kind == "light_novel"

    def test_galge_game_imports_by_tag(self):
        payload = self._game_payload(tags=[{"name": "GALGAME", "count": 88}])
        subject = import_bangumi.map_subject(payload)
        assert subject.type == "game"

    def test_galge_tag_match_is_case_insensitive(self):
        payload = self._game_payload(tags=[{"name": "galgame", "count": 5}])
        assert import_bangumi.map_subject(payload).type == "game"

    def test_plain_game_rejected(self):
        with pytest.raises(import_bangumi.SubjectMappingError) as excinfo:
            import_bangumi.map_subject(self._game_payload())
        assert "游戏" in str(excinfo.value)

    def test_related_ip_marker_imports(self):
        payload = self._game_payload(_site_related_ip=True)
        assert import_bangumi.map_subject(payload).type == "game"

    def test_marker_must_be_true(self):
        """标记只有显式 True 才放行——其它真值形态（字符串等）不认。"""
        payload = self._game_payload(_site_related_ip="yes")
        with pytest.raises(import_bangumi.SubjectMappingError):
            import_bangumi.map_subject(payload)

    def test_is_galge_payload_signals(self):
        assert import_bangumi.is_galge_payload({"tags": [{"name": "GALGAME"}]})
        assert import_bangumi.is_galge_payload({"tags": ["美少女游戏"]})
        assert import_bangumi.is_galge_payload({"tags": [{"name": "ギャルゲー"}]})
        # 视觉小说也算（2026-10-21 用户补充口径：文字冒险类一并收）
        assert import_bangumi.is_galge_payload({"tags": [{"name": "视觉小说"}]})
        assert import_bangumi.is_galge_payload({"tags": [{"name": "Visual Novel"}]})
        assert not import_bangumi.is_galge_payload({"tags": [{"name": "RPG"}]})
        assert not import_bangumi.is_galge_payload({})


# ---------- 网络层：超时、重试与代理配置 ----------


class TestFetchSubject:
    def test_request_url_headers_and_timeout(self):
        """请求 URL、User-Agent、Accept 与超时按预期传递。"""
        opener = FakeOpener([ANIME_PAYLOAD])

        payload = import_bangumi.fetch_subject(265, opener=opener, retries=0)

        assert payload == ANIME_PAYLOAD
        assert len(opener.calls) == 1
        call = opener.calls[0]
        assert call["url"] == "https://api.bgm.tv/v0/subjects/265"
        assert call["timeout"] == import_bangumi.DEFAULT_TIMEOUT
        assert call["headers"]["user-agent"] == import_bangumi.DEFAULT_USER_AGENT
        assert call["headers"]["accept"] == "application/json"

    def test_retryable_http_errors_are_retried(self):
        """500/503 等可重试状态会按次数重试，最终成功返回。"""
        opener = FakeOpener(
            [
                urllib.error.HTTPError("https://api.bgm.tv/v0/subjects/265", 500, "Internal Server Error", None, None),
                urllib.error.HTTPError("https://api.bgm.tv/v0/subjects/265", 503, "Service Unavailable", None, None),
                ANIME_PAYLOAD,
            ]
        )
        slept: list[float] = []

        payload = import_bangumi.fetch_subject(265, opener=opener, retries=2, sleep=slept.append)

        assert payload == ANIME_PAYLOAD
        assert len(opener.calls) == 3
        assert slept == [0.5, 1.0]  # 退避间隔递增

    def test_http_404_fails_fast_without_retry(self):
        """404 属于确定性失败：立即报错，不浪费重试。"""
        opener = FakeOpener(
            [urllib.error.HTTPError("https://api.bgm.tv/v0/subjects/404", 404, "Not Found", None, None)]
        )

        with pytest.raises(import_bangumi.BangumiAPIError, match="404") as excinfo:
            import_bangumi.fetch_subject(404, opener=opener, retries=3)

        assert len(opener.calls) == 1
        assert "404" in str(excinfo.value)

    def test_timeout_is_retried_then_reported(self):
        """超时会重试，重试耗尽后抛出包含明确原因的 BangumiAPIError。"""
        opener = FakeOpener([TimeoutError("timed out"), TimeoutError("timed out")])

        with pytest.raises(import_bangumi.BangumiAPIError, match="超时"):
            import_bangumi.fetch_subject(265, opener=opener, retries=1, sleep=lambda _: None)

        assert len(opener.calls) == 2

    def test_invalid_json_is_reported(self):
        """响应体不是 JSON 时报错清晰，错误信息包含条目 ID。"""
        opener = FakeOpener([b"<html>not json</html>"])

        with pytest.raises(import_bangumi.BangumiAPIError, match="JSON") as excinfo:
            import_bangumi.fetch_subject(265, opener=opener, retries=0)

        assert "265" in str(excinfo.value)


class TestProxyConfig:
    def test_api_base_env_override(self, monkeypatch):
        """BANGUMI_API_BASE 环境变量可指向镜像（服务器直连被污染时的逃生口）。"""
        monkeypatch.setenv("BANGUMI_API_BASE", "https://mirror.example/")
        _spec.loader.exec_module(import_bangumi)  # 重跑模块级代码（合成模块名不能 reload）
        try:
            assert import_bangumi.BANGUMI_API_BASE == "https://mirror.example"  # 去尾斜杠
        finally:
            monkeypatch.delenv("BANGUMI_API_BASE", raising=False)
            _spec.loader.exec_module(import_bangumi)
        assert import_bangumi.BANGUMI_API_BASE == "https://api.bgm.tv"

    def test_proxy_handler_used_when_configured(self):
        """配置代理时，opener 的 ProxyHandler 指向该代理。"""
        opener = import_bangumi.build_opener("http://127.0.0.1:9910")
        handler = next(
            h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
        )
        assert handler.proxies == {"http": "http://127.0.0.1:9910", "https": "http://127.0.0.1:9910"}

    def test_direct_connection_when_no_proxy(self):
        """默认直连：opener 上不存在任何非空代理配置，也不读系统环境变量。

        注：urllib 不会把空的 ProxyHandler({}) 注册进 opener.handlers，
        因此这里断言“没有任何指向代理的 handler”，而不是断言存在空 handler。
        """
        opener = import_bangumi.build_opener(None)
        proxy_handlers = [
            h
            for h in opener.handlers
            if isinstance(h, urllib.request.ProxyHandler) and h.proxies
        ]
        assert proxy_handlers == []

    def test_proxy_resolution_priority(self, monkeypatch):
        """--proxy 参数 > BANGUMI_PROXY 环境变量 > 直连。"""
        monkeypatch.delenv("BANGUMI_PROXY", raising=False)
        assert import_bangumi.resolve_proxy(None) is None

        monkeypatch.setenv("BANGUMI_PROXY", "http://127.0.0.1:9910")
        assert import_bangumi.resolve_proxy(None) == "http://127.0.0.1:9910"
        assert import_bangumi.resolve_proxy("http://other:8080") == "http://other:8080"

    def test_cli_defaults_to_preview_and_optional_proxy(self):
        """CLI：默认不 --apply；--proxy 与 --apply 可正确解析。"""
        preview_args = import_bangumi.build_parser().parse_args(["265", "975"])
        assert preview_args.apply is False
        assert preview_args.proxy is None
        assert preview_args.ids == [265, 975]

        apply_args = import_bangumi.build_parser().parse_args(
            ["265", "--apply", "--proxy", "http://127.0.0.1:9910"]
        )
        assert apply_args.apply is True
        assert apply_args.proxy == "http://127.0.0.1:9910"


# ---------- 入库流程：预览 / 写入 / 重复 / 失败 ----------


class TestRunImport:
    def test_preview_mode_does_not_write(self, tmp_path):
        """默认预览：结果计入 preview，数据库行数与旧数据完全不变。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 975: GAME_PAYLOAD})

        summary = import_bangumi.run_import(
            [265, 975], session_factory=factory, apply=False, fetch=fetch
        )

        assert summary.requested == 2
        assert summary.fetched == 2
        assert summary.imported == 0
        assert summary.preview == 2
        assert summary.importable == 2

        rows = all_contents(factory)
        assert len(rows) == len(FAKE_CONTENTS)
        assert all(row.bangumi_id is None and row.source_url is None for row in rows)

    def test_apply_mode_writes_mapped_rows_and_keeps_old_data(self, tmp_path):
        """--apply：新记录按映射写入；原有 6 条示例数据不被覆盖。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 975: GAME_PAYLOAD})

        summary = import_bangumi.run_import(
            [265, 975], session_factory=factory, apply=True, fetch=fetch
        )

        assert summary.imported == 2
        assert summary.preview == 0

        rows = all_contents(factory)
        assert [row.id for row in rows][: len(FAKE_CONTENTS)] == [1, 2, 3, 4, 5, 6]
        for old_row, expected in zip(rows[: len(FAKE_CONTENTS)], FAKE_CONTENTS):
            assert old_row.title == expected["title"]
            assert old_row.rating == expected["rating"]
            assert old_row.bangumi_id is None

        imported = {row.bangumi_id: row for row in rows[len(FAKE_CONTENTS):]}
        assert set(imported) == {265, 975}
        assert imported[265].title == "圣斗士星矢"
        assert imported[265].type == "anime"
        assert imported[265].rating == pytest.approx(8.7)
        assert imported[265].source_url == "https://bgm.tv/subject/265"
        assert imported[975].title == "Undertale"
        assert imported[975].type == "game"

    def test_apply_mode_writes_tags_with_content(self, tmp_path):
        """--apply：标签随作品一起入库（按标注数前 5），关联行可查。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 975: GAME_PAYLOAD})

        summary = import_bangumi.run_import(
            [265, 975], session_factory=factory, apply=True, fetch=fetch
        )

        assert summary.imported == 2
        db = factory()
        try:
            row = db.scalar(select(Content).where(Content.bangumi_id == 265))
            assert crud.list_content_tags(db, row.id) == [
                "热血", "科幻", "机甲", "战斗", "友情",
            ]
            # 975 的 payload 没有 tags：作品正常入库，只是没有标签
            row2 = db.scalar(select(Content).where(Content.bangumi_id == 975))
            assert crud.list_content_tags(db, row2.id) == []
            # 标签按名称全局去重：两个条目共 5 个标签行
            assert db.scalar(select(func.count()).select_from(Tag)) == 5
        finally:
            db.close()

    def test_duplicate_import_does_not_stack_tags(self, tmp_path):
        """重复导入同一 bangumi_id：跳过不覆盖，标签关联行不堆叠。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD})
        import_bangumi.run_import(
            [265], session_factory=factory, apply=True, fetch=fetch
        )
        summary = import_bangumi.run_import(
            [265], session_factory=factory, apply=True, fetch=fetch
        )

        assert summary.duplicates == 1
        db = factory()
        try:
            row = db.scalar(select(Content).where(Content.bangumi_id == 265))
            assert crud.list_content_tags(db, row.id) == [
                "热血", "科幻", "机甲", "战斗", "友情",
            ]
            # 关联行仍是 5 行：跳过语义对标签同样生效
            assert db.scalar(select(func.count()).select_from(ContentTag)) == 5
        finally:
            db.close()

    def test_duplicate_bangumi_id_is_skipped_without_overwrite(self, tmp_path):
        """bangumi_id 已存在：跳过且不覆盖本地内容，其余条目正常导入。"""
        engine, factory = seeded_db(tmp_path)
        db = factory()
        try:
            db.add(
                Content(
                    title="本地已有的动画条目",
                    type="anime",
                    summary="本地维护的简介，不应被导入覆盖。",
                    rating=6.0,
                    bangumi_id=265,
                    source_url="https://bgm.tv/subject/265",
                )
            )
            db.commit()
        finally:
            db.close()

        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 975: GAME_PAYLOAD})
        summary = import_bangumi.run_import(
            [265, 975], session_factory=factory, apply=True, fetch=fetch
        )

        assert summary.duplicates == 1
        assert summary.imported == 1
        duplicate = next(o for o in summary.outcomes if o.status == "duplicate")
        assert duplicate.bangumi_id == 265
        assert "不覆盖" in duplicate.reason

        rows = all_contents(factory)
        kept = next(row for row in rows if row.bangumi_id == 265)
        assert kept.title == "本地已有的动画条目"  # 未被 API 数据覆盖
        assert kept.rating == 6.0
        # 6 条种子 + 预置的重复记录 + 导入的 975 = 8；导入没有额外新增 265
        assert len(rows) == len(FAKE_CONTENTS) + 2

    def test_failed_fetch_and_unsupported_type_are_counted(self, tmp_path):
        """获取失败与类型不支持分别计入 failed / skipped，不中断其余条目。

        用「音乐」当不支持类型：书籍(1)已支持为 novel，不能再拿它当反例。
        """
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 1224: make_payload(1224, 3)})

        summary = import_bangumi.run_import(
            [111, 265, 1224],  # 111 触发模拟网络失败；1224 是音乐类
            session_factory=factory,
            apply=True,
            fetch=fetch,
        )

        assert summary.fetched == 2
        assert summary.failed == 1
        assert summary.skipped == 1
        assert summary.imported == 1
        failed = next(o for o in summary.outcomes if o.status == "failed")
        assert failed.bangumi_id == 111
        assert "失败" in failed.reason
        skipped = next(o for o in summary.outcomes if o.status == "skipped")
        assert "音乐" in skipped.reason

    def test_invalid_ids_reported_as_failed(self, tmp_path):
        """0、负数等非法 ID：计入 failed，且不会发起任何 API 请求。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({})

        summary = import_bangumi.run_import(
            [0, -5], session_factory=factory, apply=True, fetch=fetch
        )

        assert summary.failed == 2
        assert summary.fetched == 0
        assert fetch.calls == []

    def test_database_write_error_is_reported_and_rolled_back(self, tmp_path):
        """数据库写入错误：该条目计入 failed，回滚后其余数据不受影响。"""

        class BrokenCommitSession(Session):
            def commit(self):
                raise OperationalError("INSERT INTO contents", {}, OSError("disk I/O error"))

        engine, factory = seeded_db(tmp_path)
        broken_factory = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, class_=BrokenCommitSession
        )
        fetch = make_stub_fetch({265: ANIME_PAYLOAD})

        summary = import_bangumi.run_import(
            [265], session_factory=broken_factory, apply=True, fetch=fetch
        )

        assert summary.failed == 1
        outcome = summary.outcomes[0]
        assert outcome.status == "failed"
        assert "数据库错误" in outcome.reason
        assert len(all_contents(factory)) == len(FAKE_CONTENTS)  # 回滚生效

    def test_unique_index_conflict_reported_as_duplicate(self, tmp_path):
        """并发兜底：查重查询未命中、提交时撞唯一索引时按 duplicate 处理并回滚。"""
        engine, factory = seeded_db(tmp_path)
        db = factory()
        try:
            db.add(
                Content(
                    title="并发插入的条目",
                    type="anime",
                    summary="与导入同 bangumi_id 的本地记录。",
                    rating=7.0,
                    bangumi_id=265,
                )
            )
            db.commit()
        finally:
            db.close()

        class BlindSession(Session):
            """模拟查重窗口：scalar 查重永远返回“不存在”，逼真还原并发竞争。"""

            def scalar(self, *args, **kwargs):
                return None

        blind_factory = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, class_=BlindSession
        )
        db = blind_factory()
        try:
            outcome = import_bangumi._map_and_store(db, ANIME_PAYLOAD, apply=True)
        finally:
            db.close()

        assert outcome.status == "duplicate"
        assert "唯一索引" in outcome.reason
        # 仍是 7 条： IntegrityError 已回滚，本地并发记录未被覆盖
        rows = all_contents(factory)
        assert len(rows) == len(FAKE_CONTENTS) + 1
        assert next(row for row in rows if row.bangumi_id == 265).title == "并发插入的条目"

    def test_summary_text_covers_all_counts(self, tmp_path):
        """汇总输出包含获取、可导入、重复、跳过、失败等关键信息。"""
        _, factory = seeded_db(tmp_path)
        fetch = make_stub_fetch({265: ANIME_PAYLOAD, 975: GAME_PAYLOAD})

        summary = import_bangumi.run_import(
            [111, 265, 975], session_factory=factory, apply=False, fetch=fetch
        )
        text = summary.describe()

        assert "请求条目：3" in text
        assert "成功获取：2" in text
        assert "预览未写入 2" in text
        assert "失败：1" in text
        assert len(summary.outcomes) == 3


# ---------- 模型与页面接入 ----------


class TestContentModelAndPages:
    def test_model_declares_bangumi_columns_matching_db(self, tmp_path):
        """create_all 生成的表结构与既有库一致：两列存在且唯一索引同名。"""
        engine, _ = make_db(tmp_path, "schema.db")
        columns = {col["name"] for col in inspect(engine).get_columns("contents")}
        assert {"bangumi_id", "source_url"} <= columns

        indexes = {
            ix["name"]: ix["unique"] for ix in inspect(engine).get_indexes("contents")
        }
        # SQLite 自省把 unique 返回为 1/0，与 True 等值比较即可
        assert indexes.get("uq_contents_bangumi_id") == True  # noqa: E712

    def test_bangumi_id_uniqueness_enforced(self, tmp_path):
        """同一 bangumi_id 插入两次触发唯一索引冲突。"""
        _, factory = make_db(tmp_path, "unique.db")
        db = factory()
        try:
            db.add(
                Content(title="第一条", type="anime", summary="简介", rating=8.0, bangumi_id=1)
            )
            db.commit()
            db.add(
                Content(title="第二条", type="anime", summary="简介", rating=8.0, bangumi_id=1)
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        finally:
            db.close()

    def test_as_dict_keeps_backward_compatible_contract(self):
        """as_dict 不新增/删除字段：既有 API JSON 契约与旧测试保持不变。"""
        content = Content(
            id=9,
            title="标题",
            type="anime",
            summary="简介",
            rating=8.0,
            bangumi_id=265,
            source_url="https://bgm.tv/subject/265",
        )
        assert content.as_dict() == {
            "id": 9,
            "title": "标题",
            "type": "anime",
            "summary": "简介",
            "rating": 8.0,
        }

    def test_detail_page_hides_source_link_when_present(self, tmp_path):
        """详情页：source_url 仅作内部溯源字段，前台不渲染来源链接（去显著化）。"""
        _, factory = seeded_db(tmp_path)
        db = factory()
        try:
            content = Content(
                title="来自 Bangumi 的动画",
                type="anime",
                summary="带来源链接的导入条目。",
                rating=8.7,
                bangumi_id=265,
                source_url="https://bgm.tv/subject/265",
            )
            db.add(content)
            db.commit()
            content_id = content.id
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
        try:
            resp = client.get(f"/contents/{content_id}")
            assert resp.status_code == 200
            assert "来自 Bangumi 的动画" in resp.text
            # 去显著化：bgm.tv 来源链接与 Bangumi ID 都不再出现在前台
            assert "https://bgm.tv/subject/265" not in resp.text
            assert "Bangumi ID" not in resp.text
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_detail_page_without_source_url_still_works(self, tmp_path):
        """旧作品没有来源链接：页面正常渲染，不出现来源区块。"""
        _, factory = seeded_db(tmp_path)

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        client = TestClient(app)
        app.dependency_overrides[get_db] = override_get_db
        try:
            resp = client.get("/contents/2")
            assert resp.status_code == 200
            assert "漫画屋的日常" in resp.text
            assert "来源：" not in resp.text
        finally:
            app.dependency_overrides.pop(get_db, None)
