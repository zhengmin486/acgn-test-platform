"""书籍细分测试（scripts/import_bangumi.classify_book）。

背景（2026-10-19）：Bangumi 书籍区的 platform「小说」覆盖所有小说，早期导入把
它们一律映射成 novel，站内又把 novel 标成「轻小说」，导致线上轻小说区混进
《卡拉马佐夫兄弟》《三国演义》《红楼梦》等普通书籍。

站的口径是：**「文学」是标注，不是第五种类型**——日本普通小说同样落在 novel，
只是多一枚 `book_kind=literature` 的角标；非日本语种的小说才不从本站收录。
所以 classify_book 返回的是 `(content_type, book_kind)` 二元组：

    轻小说 -> (novel, light_novel)｜日本文学 -> (novel, literature)｜其余 -> (None, None)

测试全部离线：payload 是手写 fixture，不访问网络、不写数据库。
"""

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "import_bangumi.py"

_spec = importlib.util.spec_from_file_location("import_bangumi_book_classify", SCRIPT_PATH)
import_bangumi = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = import_bangumi
_spec.loader.exec_module(import_bangumi)

LIGHT_NOVEL = ("novel", "light_novel")
LITERATURE = ("novel", "literature")
DROPPED = (None, None)


def book(**kwargs) -> dict:
    """构造一个 type=1 书籍 payload；platform 默认「小说」。"""
    payload = {"type": 1, "platform": "小说", "name": "测试条目"}
    payload.update(kwargs)
    return payload


class TestClassifyBook:
    """书籍细分：标签 / 文库字段 -> 轻小说，日本文学 -> 文学标注，其余丢弃。"""

    def test_light_novel_by_tag(self):
        payload = book(tags=[{"name": "科幻", "count": 120}, {"name": "轻小说", "count": 9}])
        assert import_bangumi.classify_book(payload) == LIGHT_NOVEL

    def test_light_novel_by_infobox_bunko_key(self):
        payload = book(infobox=[{"key": "文库", "value": "富士見ファンタジア文庫"}])
        assert import_bangumi.classify_book(payload) == LIGHT_NOVEL

    def test_light_novel_by_known_bunko_value(self):
        payload = book(infobox=[{"key": "作者", "value": "鎌池和馬"}, {"key": "出版社", "value": "電撃文庫"}])
        assert import_bangumi.classify_book(payload) == LIGHT_NOVEL

    def test_japanese_plain_novel_is_literature(self):
        """日本普通小说：没有文库信号，但有日系出版社 -> 文学标注。"""
        payload = book(infobox=[{"key": "作者", "value": "田中芳樹"}, {"key": "出版社", "value": "徳間書店"}])
        assert import_bangumi.classify_book(payload) == LITERATURE

    def test_literature_keeps_the_novel_type(self):
        """文学不是第五种类型：类型仍是 novel，只有标注不同。"""
        payload = book(infobox=[{"key": "作者", "value": "貴志祐介"}])
        content_type, kind = import_bangumi.classify_book(payload)
        assert content_type == "novel"
        assert kind == "literature"

    def test_unknown_publisher_stays_as_literature(self):
        """判不出国籍时不丢书：宁可在「文学」里留待复核，也不误删日本书。"""
        payload = book(infobox=[{"key": "作者", "value": "貴志祐介"}])
        assert import_bangumi.classify_book(payload) == LITERATURE

    def test_chinese_classic_is_dropped(self):
        payload = book(infobox=[{"key": "作者", "value": "鲁迅"}, {"key": "出版社", "value": "人民文学出版社"}])
        assert import_bangumi.classify_book(payload) == DROPPED

    def test_translated_western_classic_is_dropped(self):
        payload = book(infobox=[{"key": "作者", "value": "陀思妥耶夫斯基"}, {"key": "出版社", "value": "上海译文出版社"}])
        assert import_bangumi.classify_book(payload) == DROPPED

    def test_empty_payload_is_kept_as_literature(self):
        """完全没信息时不丢书——留给「文学」区人工复核。"""
        assert import_bangumi.classify_book(book()) == LITERATURE

    def test_broken_field_types_do_not_crash(self):
        payload = book(tags="not-a-list", infobox=[{"key": "作者", "value": [""]}])
        assert import_bangumi.classify_book(payload) == LITERATURE


class TestResolveBook:
    """resolve_book / resolve_content_type 在书籍区的分支。"""

    def test_comic_platform(self):
        assert import_bangumi.resolve_book(book(platform=1001)) == ("comic", None)

    def test_comic_platform_string(self):
        assert import_bangumi.resolve_book(book(platform="漫画")) == ("comic", None)

    def test_novel_platform_goes_through_classifier(self):
        payload = book(platform="轻小说")
        assert import_bangumi.resolve_book(payload) == LIGHT_NOVEL

    def test_numeric_novel_platform_uses_signals(self):
        payload = book(platform=1002, tags=[{"name": "轻小说", "count": 3}])
        assert import_bangumi.resolve_book(payload) == LIGHT_NOVEL

    def test_non_japanese_novel_platform_is_dropped(self):
        payload = book(platform=1002, infobox=[{"key": "出版社", "value": "译林出版社"}])
        assert import_bangumi.resolve_book(payload) == DROPPED

    def test_artbook_platform_is_dropped(self):
        assert import_bangumi.resolve_book(book(platform=1003)) == DROPPED

    def test_non_book_types_have_no_kind(self):
        """非书籍条目不带标注：book_kind 是书籍专属概念。"""
        assert import_bangumi.resolve_book({"type": 2}) == ("anime", None)
        assert import_bangumi.resolve_book({"type": 4}) == ("game", None)
        assert import_bangumi.resolve_book({"type": 3}) == DROPPED
        assert import_bangumi.resolve_book({"type": 6}) == DROPPED

    def test_resolve_content_type_stays_string(self):
        """旧调用方只拿类型的契约不变。"""
        assert import_bangumi.resolve_content_type(book(platform=1002)) == "novel"
        assert import_bangumi.resolve_content_type({"type": 2}) == "anime"


class TestMapSubjectBookKind:
    """map_subject 把标注带上，落到 MappedSubject.book_kind。"""

    def test_literature_kind_is_carried(self):
        payload = {
            "id": 1,
            "type": 1,
            "platform": "小说",
            "name": "人間失格",
            "summary": "太宰治的中篇。",
            "rating": {"score": 8.1},
            "infobox": [{"key": "作者", "value": "太宰治"}, {"key": "出版社", "value": "新潮社"}],
        }
        subject = import_bangumi.map_subject(payload)
        assert subject.type == "novel"
        assert subject.book_kind == "literature"

    def test_light_novel_kind_is_carried(self):
        payload = {
            "id": 2,
            "type": 1,
            "platform": "小说",
            "name": "とある魔術の禁書目録",
            "summary": "鎌池和馬的轻小说。",
            "rating": {"score": 7.6},
            "tags": [{"name": "轻小说", "count": 30}],
        }
        subject = import_bangumi.map_subject(payload)
        assert subject.type == "novel"
        assert subject.book_kind == "light_novel"

    def test_non_book_entry_has_no_kind(self):
        payload = {
            "id": 3,
            "type": 2,
            "name": "某动画",
            "summary": "动画简介。",
            "rating": {"score": 7.0},
        }
        assert import_bangumi.map_subject(payload).book_kind is None

    def test_non_japanese_novel_is_skipped(self):
        """非日本语种小说：映射直接抛「跳过」，不进负庫。"""
        payload = {
            "id": 4,
            "type": 1,
            "platform": "小说",
            "name": "罪与罚",
            "summary": "陀思妥耶夫斯基的长篇。",
            "rating": {"score": 9.0},
            "infobox": [{"key": "出版社", "value": "人民文学出版社"}],
        }
        try:
            import_bangumi.map_subject(payload)
        except import_bangumi.SubjectMappingError:
            return
        raise AssertionError("非日本语种小说应当被 map_subject 跳过")
