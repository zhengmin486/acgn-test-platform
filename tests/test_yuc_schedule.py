"""新番档期模块测试（Yuc.wiki 同步 + 页面 + API）。

原则：
- 外部网站全部用固定 ATOM/HTML fixture 与假 opener 模拟，不访问真实网络；
- 数据库使用独立临时 SQLite（tmp_path），不触碰 local_dev.db；
- 不执行正式同步 --apply 的真实调用。
"""

import importlib.util
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import crud
from app.database import Base, build_engine, get_db
from app.main import app
from app.models import AnimeSchedule, Content, ScheduleSyncRun
from auth_helpers import create_accounts, login_as_admin

# scripts/ 不是包，按文件路径加载被测模块（与直接运行脚本等价）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "sync_yuc_schedule.py"

_spec = importlib.util.spec_from_file_location("sync_yuc_schedule_for_tests", SCRIPT_PATH)
sync_yuc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync_yuc  # dataclass 依赖模块注册到 sys.modules
_spec.loader.exec_module(sync_yuc)

ATOM_URL = "https://yuc.wiki/atom.xml"
SEASON_URL = "http://yuc.wiki/202610/"

# ---------- 固定 fixture（镜像真实页面结构） ----------

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>長門番堂</title>
  <entry><title>新番卫星观测站</title><link href="http://yuc.wiki/new/"/><updated>2026-10-11T14:26:57.042Z</updated></entry>
  <entry><title>2026年7月新番表</title><link href="http://yuc.wiki/202607/"/><updated>2026-09-01T00:00:00.000Z</updated></entry>
  <entry><title>2026年10月新番表</title><link href="http://yuc.wiki/202610/"/><updated>2026-08-01T00:00:00.000Z</updated></entry>
  <entry><title>Movie/OVA/OAD/SP etc.</title><link href="http://yuc.wiki/sp/"/><updated>2026-09-20T18:50:24.545Z</updated></entry>
  <entry><title>内地上映动画电影</title><link href="http://yuc.wiki/cnmovie/"/><updated>2026-09-21T10:00:00.000Z</updated></entry>
  <entry><title>2025年1月新番表</title><link href="http://yuc.wiki/202501/"/><updated>2026-10-12T00:00:00.000Z</updated></entry>
</feed>"""


def build_entry(
    num: int,
    *,
    cn: str | None,
    jp: str | None,
    broadcast: str = "10/3周六深夜",
    broadcast_ex: str = "<!--(ex)-->",
    links_a=(("动画官网", "https://anime-example.jp/"), ("PV", "https://www.bilibili.com/video/BVxxx/")),
    links_b: str = "",
    image: bool = True,
) -> str:
    """按 Yuc.wiki 真实条目结构构造一个作品块。"""
    poster = (
        f'<div style="float:left"><img width="180px" data-src="https://img.example/poster{num}.jpg" '
        'referrerPolicy="no-referrer"></div>'
        if image
        else ""
    )
    cn_html = f'<p class="title_cn_r">{cn}</p>' if cn is not None else ""
    jp_html = f'<p class="title_jp_r">{jp}</p>' if jp is not None else ""
    links_a_html = "".join(f'<a href="{href}" target="_blank">{text}</a><br>' for text, href in links_a)
    return (
        f"<!--#A{num:02d}-->\n{poster}\n"
        '<div><table width="500px"><tr><td class="title_main_r" colspan="2" rowspan="2">\n'
        f"{cn_html}\n{jp_html}</td>\n"
        '<td class="type_a_r">原创动画</td></tr>\n'
        '<tr><td class="type_tag_r">科幻</td></tr><tr>\n'
        '<td rowspan="2" class="staff_r">导演：某人</td>\n'
        '<td rowspan="2" class="cast_r">声优们</td>\n'
        '<td class="link_a_r">\n'
        f"{links_a_html}\n"
        f'<p class="broadcast_r">{broadcast}</p>\n'
        f'<p class="broadcast_ex_r">{broadcast_ex}</p></td></tr>\n'
        f'<tr><td class="link_b_r">{links_b}</td></tr></table></div>\n'
        '<div style="clear:both"></div>\n'
    )


PAGE_HEADER = """<!DOCTYPE html><html><head><title>2026年10月新番表 - 長門番堂</title></head><body>
<p class="intro">本期 <b>秋季档</b> 共收录 4 部新番动画</p>
<div style="float:left"><div class="div_date_"><img width="120px" data-src="https://img.example/calendar.jpg"></div>
<div><table width="120px" height="40px"><tr><td colspan="2" class="date_title_">赛博朋克 边缘行者2</td></tr></table></div></div>
"""


def build_season_page(
    *,
    a1_broadcast: str = "10/3周六深夜",
    extra_entry: str = "",
) -> str:
    """完整档期页：头部日历区 + 4 个详情条目（A03 缺中文名/海报/合法链接）。"""
    a01 = build_entry(
        1,
        cn="顶点武装",
        jp="VERTEX FORCE",
        broadcast=a1_broadcast,
    )
    a02 = build_entry(
        2,
        cn="银翼骑士",
        jp="銀翼のナイト",
        broadcast="每周六 24:30 网络放送",
        broadcast_ex="(全12话)",
        links_b='<a href="https://www.bilibili.com/" target="_blank">bilibili</a>',
    )
    a03 = build_entry(
        3,
        cn=None,
        jp="ミニスカ忍者隊",
        broadcast="10/5 深夜",
        links_a=(("公式", "https://anime-example.jp/minininja/"),),
        links_b=(
            '<a href="javascript:void(0)" target="_blank">坏链接</a>'
            '<a href="https://www.iqiyi.com/" target="_blank">爱奇艺</a>'
        ),
        image=False,
    )
    a04 = build_entry(
        4,
        cn="顶点武装",
        jp="顶点武装剧场版",
        image=True,
    )
    return PAGE_HEADER + a01 + a02 + a03 + a04 + extra_entry + "</body></html>"


SEASON_HTML = build_season_page()
SEASON_HTML_V2 = build_season_page(
    a1_broadcast="10/4周日 23:00 再放送",
    extra_entry=build_entry(5, cn="新加入的作品", jp="NEW ENTRY", broadcast="周一 25:00"),
)


def make_stub_fetch(pages: dict[str, str]):
    """用字典实现的假 fetch(url)；未命中的 URL 报“请求失败”。"""
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        if url not in pages:
            raise sync_yuc.YucFetchError(f"请求 {url} 失败：HTTP 404 Not Found")
        return pages[url]

    fetch.calls = calls
    return fetch


def make_db(tmp_path, name: str = "schedule.db"):
    """每个用例一个独立临时 SQLite，返回 session 工厂；已建全部表。"""
    engine = build_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def upsert_sample(db, season="202610", **overrides):
    """直接向临时库写入一条样例档期记录，返回 (记录, 动作)。"""
    data = {
        "season_code": season,
        "source_key": "样例作品",
        "title_main": "样例作品",
        "title_cn": "样例作品",
        "title_jp": "サンプル",
        "weekday": 6,
        "broadcast_time": "24:30",
        "broadcast_note": "每周六 24:30",
        "image_url": "https://img.example/sample.jpg",
        "platforms": [{"name": "bilibili", "url": "https://www.bilibili.com/"}],
        "source_url": f"https://yuc.wiki/{season}/",
    }
    data.update(overrides)
    return crud.upsert_schedule(db, data)


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """按脚本逐次返回响应或抛异常的假 opener，并记录每次请求。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if not self._script:
            raise AssertionError("脚本已耗尽，收到了多余的请求")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)


# ---------- Atom 档期发现 ----------


class TestAtomDiscovery:
    def test_finds_and_sorts_seasons_by_year_month(self):
        """识别 /YYYYMM/ 档期条目，按年月新到旧排序；非档期条目跳过。"""
        seasons = sync_yuc.parse_atom(ATOM_XML)

        assert [s.code for s in seasons] == ["202610", "202607", "202501"]
        assert seasons[0].title == "2026年10月新番表"
        assert seasons[0].url == SEASON_URL
        codes = {s.code for s in seasons}
        assert "new" not in codes and "sp" not in codes

    def test_latest_pick_uses_year_month_not_update_time(self):
        """2025年1月条目 updated 最新，但最新档期必须是 2026年10月。"""
        seasons = sync_yuc.parse_atom(ATOM_XML)
        assert sync_yuc.pick_season(seasons, None).code == "202610"

    def test_pick_season_explicit_hit(self):
        seasons = sync_yuc.parse_atom(ATOM_XML)
        assert sync_yuc.pick_season(seasons, "202607").code == "202607"

    def test_pick_season_miss_raises_with_available_list(self):
        seasons = sync_yuc.parse_atom(ATOM_XML)
        with pytest.raises(sync_yuc.SeasonNotFoundError) as excinfo:
            sync_yuc.pick_season(seasons, "199901")
        assert "199901" in str(excinfo.value)
        assert [s.code for s in excinfo.value.available] == ["202610", "202607", "202501"]

    def test_invalid_atom_raises_clear_error(self):
        with pytest.raises(sync_yuc.YucFetchError, match="Atom"):
            sync_yuc.parse_atom("<this is not xml")


class TestSpecialCategories:
    """特殊档期分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）的发现与改写。"""

    def test_discovers_three_special_categories_with_relabeled_titles(self):
        """从 Atom 认出三个特殊分类，并把站点原文改写为本站点名。"""
        specials = sync_yuc.parse_special_categories(ATOM_XML)
        by_code = {s.code: s for s in specials}
        assert set(by_code) == {"unreleased", "ova_sp", "cn_movie"}
        # 站点原文（新番卫星观测站 / Movie-OVA-SP / 内地上映动画电影）已改写
        assert by_code["unreleased"].title == "未上线新番"
        assert by_code["unreleased"].url == "http://yuc.wiki/new/"
        assert by_code["ova_sp"].title == "剧场版ova与特别篇"
        assert by_code["ova_sp"].url == "http://yuc.wiki/sp/"
        assert by_code["cn_movie"].title == "大陆引进剧场版"
        assert by_code["cn_movie"].url == "http://yuc.wiki/cnmovie/"

    def test_parse_atom_still_only_seasons(self):
        """parse_atom 仍只返回季度档期，特殊分类不混进季度发现结果。"""
        seasons = sync_yuc.parse_atom(ATOM_XML)
        assert [s.code for s in seasons] == ["202610", "202607", "202501"]
        codes = {s.code for s in seasons}
        assert "new" not in codes and "sp" not in codes and "cnmovie" not in codes

    def test_special_category_by_path_and_title(self):
        """路径片段（/new/、/sp/）优先；拿不到路径时按标题子串兜底。"""
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><title>新番卫星观测站</title><link href="http://yuc.wiki/new/"/></entry>'
            '<entry><title>Movie/OVA/OAD/SP etc.</title><link href="http://yuc.wiki/sp/"/></entry>'
            '<entry><title>内地上映动画电影</title><link href="http://yuc.wiki/cnmovie/"/></entry>'
            "</feed>"
        )
        specials = {s.code: s for s in sync_yuc.parse_special_categories(atom)}
        assert set(specials) == {"unreleased", "ova_sp", "cn_movie"}

    def test_find_special_category_locates_page(self):
        """find_special_category：按分类码定位页面地址与本站点名。"""
        info = sync_yuc.find_special_category(ATOM_XML, "cn_movie")
        assert info.url == "http://yuc.wiki/cnmovie/"
        assert info.title == "大陆引进剧场版"

    def test_find_special_category_raises_when_missing(self):
        """分类码不存在时抛 SeasonNotFoundError（页面据此回显原因）。"""
        with pytest.raises(sync_yuc.SeasonNotFoundError):
            sync_yuc.find_special_category(ATOM_XML, "not_a_category")

    def test_run_sync_special_category_stores_category(self, tmp_path):
        """run_sync_special_category：先读 Atom 定位分类页，再带 category 写库。"""
        factory = make_db(tmp_path)
        requested: list[str] = []

        def fake_fetch(url):
            requested.append(url)
            if url.endswith("/atom.xml"):
                return ATOM_XML
            return SEASON_HTML

        summary = sync_yuc.run_sync_special_category(
            "unreleased",
            session_factory=factory,
            apply=True,
            fetch=fake_fetch,
        )
        assert summary.added >= 1
        # 先读 Atom，再抓分类页
        assert requested[0].endswith("/atom.xml")
        assert "http://yuc.wiki/new/" in requested
        # 落库带 category，且季度档期列表不含该分类
        db = factory()
        try:
            rows = db.query(AnimeSchedule).all()
            assert rows and all(row.category == "unreleased" for row in rows)
            assert all(row.season_code == "unreleased" for row in rows)
            seasons = crud.list_schedule_seasons(db)
            assert seasons == []
        finally:
            db.close()


# ---------- 字段清洗与映射 ----------


class TestFieldExtraction:
    def test_clean_and_normalize(self):
        assert sync_yuc.clean_text("  顶点\u3000武装 \n") == "顶点 武装"
        assert sync_yuc.clean_text("   ") is None
        assert sync_yuc.normalize_title_key("赛博朋克 边缘行者2 ") == "赛博朋克边缘行者2"
        assert sync_yuc.normalize_title_key(None) is None

    def test_class_token_normalization(self):
        """站点给同类名追加数字序号（title_cn_r1…），且 2020 老页面无 _r 后缀。

        title_cn_r1 / title_cn_r / title_cn 三种写法必须归一为同一个类名。
        """
        assert sync_yuc.normalize_class_token("title_cn_r") == "title_cn"
        assert sync_yuc.normalize_class_token("title_cn_r1") == "title_cn"
        assert sync_yuc.normalize_class_token("title_jp_r23") == "title_jp"
        assert sync_yuc.normalize_class_token("title_cn") == "title_cn"
        assert sync_yuc.normalize_class_token("broadcast") == "broadcast"
        assert sync_yuc.class_tokens({"class": "title_cn_r2 foo"}) == {"title_cn", "foo"}
        # 2020 老模板：无 _r 后缀、无序号
        assert sync_yuc.class_tokens({"class": "link_b"}) == {"link_b"}

    @pytest.mark.parametrize(
        ("text", "weekday", "clock"),
        [
            ("10/3周六深夜", 6, None),
            ("每周六 24:30 网络放送", 6, "24:30"),
            ("日曜日 18:00", 7, "18:00"),
            ("10/5 深夜", None, None),
            (None, None, None),
        ],
    )
    def test_weekday_and_time(self, text, weekday, clock):
        assert sync_yuc.extract_weekday(text) == weekday
        assert sync_yuc.extract_broadcast_time(text) == clock

    def test_platform_link_rules(self):
        bilibili = sync_yuc.platform_from_link("bilibili", "https://www.bilibili.com/")
        assert bilibili == {"name": "bilibili", "url": "https://www.bilibili.com/"}

        # PV / 非平台域名 / 非 http 链接都不算播放平台
        assert sync_yuc.platform_from_link("PV", "https://www.bilibili.com/video/BV1/") is None
        assert sync_yuc.platform_from_link("官网", "https://anime-example.jp/") is None
        assert sync_yuc.platform_from_link("坏链接", "javascript:void(0)") is None
        assert sync_yuc.platform_from_link("相对", "/relative/path") is None

        # 无锚文本时回退用域名命名
        netflix = sync_yuc.platform_from_link("", "https://www.netflix.com/title/1")
        assert netflix["name"] == "www.netflix.com"


# ---------- HTML 条目解析 ----------


class TestEntryParsing:
    def test_full_entry_mapping(self):
        """主/中/日标题、星期、时间、原始说明、海报、平台、来源各归其位。"""
        chunks = sync_yuc.split_entries(SEASON_HTML)
        assert len(chunks) == 4

        entry = sync_yuc.parse_entry_chunk(chunks[0].html)
        assert entry.title_cn == "顶点武装"
        assert entry.title_jp == "VERTEX FORCE"
        assert entry.title_main == "顶点武装"
        assert entry.weekday == 6
        assert entry.broadcast_time is None
        assert entry.broadcast_note == "10/3周六深夜"
        assert entry.image_url == "https://img.example/poster1.jpg"
        # 官网/PV 链接不是平台；PV 虽在 bilibili 域名下也被排除
        assert entry.platforms == []

    def test_entry_with_platform_and_time(self):
        chunks = sync_yuc.split_entries(SEASON_HTML)
        entry = sync_yuc.parse_entry_chunk(chunks[1].html)
        assert entry.title_cn == "银翼骑士"
        assert entry.weekday == 6
        assert entry.broadcast_time == "24:30"
        assert entry.broadcast_note == "每周六 24:30 网络放送；(全12话)"
        assert entry.platforms == [{"name": "bilibili", "url": "https://www.bilibili.com/"}]

    def test_entry_missing_fields_and_invalid_links(self):
        """缺中文名回退日文名；无海报为空；非法链接被跳过，平台只留可确认的。"""
        chunks = sync_yuc.split_entries(SEASON_HTML)
        entry = sync_yuc.parse_entry_chunk(chunks[2].html)
        assert entry.title_cn is None
        assert entry.title_main == "ミニスカ忍者隊"
        assert entry.weekday is None
        assert entry.broadcast_time is None
        assert entry.image_url is None
        assert entry.platforms == [{"name": "爱奇艺", "url": "https://www.iqiyi.com/"}]

    def test_work_type_and_tags_extracted(self):
        """作品类型与题材标签从 td.type_*_r / td.type_tag_r 提取。"""
        chunks = sync_yuc.split_entries(SEASON_HTML)
        entry = sync_yuc.parse_entry_chunk(chunks[0].html)
        assert entry.work_type == "原创动画"
        assert entry.work_tags == "科幻"

    def test_suffixed_class_variants_still_parse(self):
        """站点同款类名带数字序号（title_cn_r1/r2、staff_r2、type_b_r 等）也必须解析。"""
        chunk = (
            '<p class="title_cn_r1">JOJO奇妙冒险<br>飙马野郎</p>'
            '<p class="title_jp_r2">スティール・ボール・ラン</p>'
            '<td class="type_b_r">漫画改编动画</td>'
            '<td class="type_tag_r1">战斗/冒险/赛马</td>'
            '<td class="staff_r2">原作：荒木飞吕彦</td>'
            '<p class="broadcast_r">10/3周六深夜</p>'
        )
        entry = sync_yuc.parse_entry_chunk(chunk)
        assert entry.title_cn == "JOJO奇妙冒险 飙马野郎"
        assert entry.title_jp == "スティール・ボール・ラン"
        assert entry.work_type == "漫画改编动画"
        assert entry.work_tags == "战斗/冒险/赛马"
        assert entry.weekday == 6

    def test_malformed_html_chunk_still_yields_title(self):
        """未闭合标签的畸形块：容忍并取到已出现的字段，不抛异常。

        未闭合的 <p> 内所有文本都被视为该字段内容（宽容解析的既定行为）。
        """
        entry = sync_yuc.parse_entry_chunk("<p class='title_cn_r'>未闭合标题<div><span>坏结构")
        assert entry.title_main == "未闭合标题坏结构"

    def test_empty_chunk_yields_no_title(self):
        entry = sync_yuc.parse_entry_chunk("<div></div>")
        assert entry.title_main is None

    def test_fallback_split_without_markers(self):
        """无注释标记的页面回退：按 title_main_r 切块，海报从紧邻前文提取。"""
        stripped = re.sub(r"<!--\s*#\s*[A-Za-z]+\d+\s*-->", "", SEASON_HTML)
        chunks = sync_yuc.split_entries(stripped)
        assert len(chunks) == 4
        assert all(not chunk.trust_embedded_image for chunk in chunks)
        posters = [chunk.fallback_image for chunk in chunks]
        assert posters == [
            "https://img.example/poster1.jpg",
            "https://img.example/poster2.jpg",
            None,  # A03 本来就没有海报，窗口内不应误取上一条的海报
            "https://img.example/poster4.jpg",
        ]
        entry = sync_yuc.parse_entry_chunk(
            chunks[0].html,
            fallback_image=chunks[0].fallback_image,
            allow_embedded_image=chunks[0].trust_embedded_image,
        )
        assert entry.title_main == "顶点武装"
        assert entry.image_url == "https://img.example/poster1.jpg"


# ---------- 星期区播出位（具体时间来源） ----------


def build_weekday_block(
    *,
    time: str | None = "20:30~",
    poster: str = "https://img.example/poster9.jpg",
    title: str = "某作品",
    slot_class: str = "div_date",
    time_class: str = "imgtext4",
    title_class: str = "date_title_",
    region_links=(("港台", "https://acg.gamer.com.tw/acgDetail.php?s=150292"),),
) -> str:
    """按真实页面结构构造一个星期区播出位块。"""
    time_html = f'<p class="{time_class}">{time}</p>' if time else ""
    links_html = "".join(
        f'<a href="{href}" target="_blank"><img data-src="https://img.example/area.jpg">'
        f"<br><p class=\"area\">{area}</p></a>"
        for area, href in region_links
    )
    return (
        f'<div style="float:left"><div class="{slot_class}">{time_html}'
        f'<img width="120px" data-src="{poster}" referrerPolicy="no-referrer"></div>'
        f'<div><table width="120px"><tr><td colspan="3" class="{title_class}">{title}</td></tr>'
        f'<tr class="tr_area">{links_html}</tr></table></div></div>'
    )


class TestWeekdaySlots:
    def test_slots_extracted_with_time_poster_title(self):
        page = PAGE_HEADER + build_weekday_block(time="24:35~", title="天是红河岸")
        slots = sync_yuc.parse_weekday_slots(page)
        # PAGE_HEADER 头部日历卡（div_date_）也算一个无时间播出位
        assert len(slots) == 2
        slot = slots[-1]
        assert slot.time == "24:35"
        assert slot.poster_url == "https://img.example/poster9.jpg"
        assert slot.title == "天是红河岸"
        assert slot.platforms == [
            {"name": "港台", "url": "https://acg.gamer.com.tw/acgDetail.php?s=150292"}
        ]

    def test_platform_card_div_date_has_platforms_but_no_time(self):
        """div_date_（尾部多一个下划线）是纯平台卡片：没时间，但有平台链接。"""
        page = PAGE_HEADER + build_weekday_block(
            time=None,
            slot_class="div_date_",
            title_class="date_title__",
            title="赛博朋克 边缘行者2",
            region_links=(("环大陆", "https://www.netflix.com/title/81054853"),),
        )
        slots = sync_yuc.parse_weekday_slots(page)
        assert len(slots) == 2  # 头部日历卡 + 平台卡片
        slot = slots[-1]
        assert slot.time is None
        assert slot.platforms == [
            {"name": "环大陆", "url": "https://www.netflix.com/title/81054853"}
        ]

    def test_slot_without_time_is_still_indexed_for_platforms(self):
        """无时间的播出位仍进索引（供平台关联），但时间查询返回 None。"""
        page = PAGE_HEADER + build_weekday_block(time=None, title="网络放送作品")
        index = sync_yuc.build_slot_index(sync_yuc.parse_weekday_slots(page))
        assert sync_yuc.slot_time_for(index, poster_url=None, title="网络放送作品") is None
        slot = sync_yuc.lookup_slot(index, poster_url=None, title="网络放送作品")
        assert slot is not None and slot.platforms

    def test_slot_time_matches_by_poster_then_title(self):
        slots = [
            sync_yuc.WeekdaySlot(time="23:45", poster_url="https://img.example/p1.jpg", title="作品甲"),
            sync_yuc.WeekdaySlot(time="26:10", poster_url=None, title="作品乙"),
        ]
        index = sync_yuc.build_slot_index(slots)
        assert sync_yuc.slot_time_for(index, poster_url="https://img.example/p1.jpg", title=None) == "23:45"
        # 海报缺失（如条目无海报）时按规范化标题兜底
        assert sync_yuc.slot_time_for(index, poster_url=None, title="作品 乙") == "26:10"
        # 关联不上不猜
        assert sync_yuc.slot_time_for(index, poster_url=None, title="不存在的作品") is None

    def test_slot_platforms_merged_into_entry(self):
        """条目没有平台链接时，合并星期区区域平台；非平台域名仍被过滤。"""
        index = sync_yuc.build_slot_index(
            [
                sync_yuc.WeekdaySlot(
                    time="23:45",
                    poster_url="https://img.example/p1.jpg",
                    title=None,
                    platforms=[{"name": "港台", "url": "https://acg.gamer.com.tw/d?s=1"}],
                )
            ]
        )
        chunk = (
            '<img width="180px" data-src="https://img.example/p1.jpg">'
            '<p class="title_cn_r">作品甲</p>'
            '<p class="broadcast_r">10/3周六深夜</p>'
        )
        entry = sync_yuc.parse_entry_chunk(chunk, slot_index=index)
        assert entry.broadcast_time == "23:45"
        assert entry.platforms == [{"name": "港台", "url": "https://acg.gamer.com.tw/d?s=1"}]

    def test_note_time_takes_priority_over_slot(self):
        """条目说明自带 HH:MM 时以条目为准，星期区时间只作回退。"""
        index = sync_yuc.build_slot_index(
            [sync_yuc.WeekdaySlot(time="23:45", poster_url="https://img.example/p1.jpg", title=None)]
        )
        chunk = (
            '<img width="180px" data-src="https://img.example/p1.jpg">'
            '<p class="title_cn_r">作品甲</p>'
            '<p class="broadcast_r">每周六 24:30 网络放送</p>'
        )
        entry = sync_yuc.parse_entry_chunk(chunk, slot_index=index)
        assert entry.broadcast_time == "24:30"

        chunk_without_note_time = chunk.replace("每周六 24:30 网络放送", "10/3周六深夜")
        entry = sync_yuc.parse_entry_chunk(chunk_without_note_time, slot_index=index)
        assert entry.broadcast_time == "23:45"


# ---------- 同步流程：dry-run / apply / 幂等 ----------


class TestRunSync:
    def test_dry_run_does_not_write(self, tmp_path):
        """默认预览：新番数据零写入，但运行记录（dry-run）保存。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

        summary = sync_yuc.run_sync(None, session_factory=factory, fetch=fetch)

        assert summary.mode == "dry-run"
        assert summary.season_code == "202610"
        assert summary.pages_fetched == 1
        assert summary.parsed == 4
        assert summary.valid == 3  # A04 与 A01 重复，不计入有效
        assert summary.preview == 3
        assert summary.added == summary.updated == summary.unchanged == 0

        db = factory()
        try:
            assert db.query(AnimeSchedule).count() == 0
            runs = db.query(ScheduleSyncRun).all()
            assert len(runs) == 1
            assert runs[0].mode == "dry-run"
            assert runs[0].added == 0
            assert runs[0].finished_at is not None
            assert "dry-run" in (runs[0].message or "")
        finally:
            db.close()

    def test_apply_first_sync_inserts_and_keeps_other_modules(self, tmp_path):
        """--apply：解析结果入库；Content 表数据不受影响。"""
        factory = make_db(tmp_path)
        db = factory()
        try:
            db.add(Content(title="已有内容", type="anime", summary="简介", rating=8.0))
            db.commit()
            content_before = db.query(Content).all()
        finally:
            db.close()

        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})
        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        assert (summary.added, summary.updated, summary.unchanged) == (3, 0, 0)
        assert summary.duplicates == 1

        db = factory()
        try:
            rows = {row.title_main: row for row in db.query(AnimeSchedule).all()}
            assert set(rows) == {"顶点武装", "银翼骑士", "ミニスカ忍者隊"}

            a01 = rows["顶点武装"]
            assert a01.season_code == "202610"
            assert a01.source_key == "顶点武装"
            assert a01.title_cn == "顶点武装"
            assert a01.title_jp == "VERTEX FORCE"
            assert a01.weekday == 6
            assert a01.broadcast_time is None
            assert a01.broadcast_note == "10/3周六深夜"
            assert a01.image_url == "https://img.example/poster1.jpg"
            assert a01.platforms == []
            assert a01.source_url == SEASON_URL

            a02 = rows["银翼骑士"]
            assert a02.weekday == 6
            assert a02.broadcast_time == "24:30"
            assert a02.platforms == [{"name": "bilibili", "url": "https://www.bilibili.com/"}]

            a03 = rows["ミニスカ忍者隊"]
            assert a03.title_cn is None
            assert a03.image_url is None

            run = db.query(ScheduleSyncRun).one()
            assert run.mode == "apply" and run.added == 3

            # 其他模块数据未被触碰
            content_after = db.query(Content).all()
            assert [(c.id, c.title) for c in content_after] == [
                (c.id, c.title) for c in content_before
            ]
        finally:
            db.close()

    def test_apply_backfills_time_and_work_info_from_weekday_section(self, tmp_path):
        """整页同步：具体时间从星期区播出位回填，作品类型/题材标签入库。

        无对应播出位的作品（如纯网络放送）时间保持 NULL，不猜测。
        """
        factory = make_db(tmp_path)
        page = (
            PAGE_HEADER
            + build_entry(1, cn="顶点武装", jp="VERTEX FORCE", broadcast="10/3周六深夜")
            + build_entry(6, cn="银翼骑士", jp="銀翼のナイト", broadcast="10/3周六深夜")
            + build_weekday_block(
                poster="https://img.example/poster1.jpg", title="顶点武装", time="23:45~"
            )
            + "</body></html>"
        )
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: page})
        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        # 头部日历卡（div_date_）+ 1 个真实播出位
        assert summary.weekday_slots == 2
        assert summary.added == 2

        db = factory()
        try:
            rows = {row.title_main: row for row in db.query(AnimeSchedule).all()}
            a01 = rows["顶点武装"]
            assert a01.broadcast_time == "23:45"
            assert a01.work_type == "原创动画"
            assert a01.work_tags == "科幻"
            # 条目自身没有平台链接：星期区区域平台（港台）合并进来
            assert a01.platforms == [
                {"name": "港台", "url": "https://acg.gamer.com.tw/acgDetail.php?s=150292"}
            ]
            assert rows["银翼骑士"].broadcast_time is None
        finally:
            db.close()

    def test_repeated_sync_is_idempotent(self, tmp_path):
        """重复同步：不重复新增，源数据无变化时全部记为“不变”。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})
        sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)
        assert (summary.added, summary.updated, summary.unchanged) == (0, 0, 3)

        db = factory()
        try:
            assert db.query(AnimeSchedule).count() == 3
        finally:
            db.close()

    def test_apply_special_category_sync_stores_category(self, tmp_path):
        """特殊分类同步：season_code / category 都写为分类码，站点原文改写为本站点名。

        用 build_entry 拼一份与季度页同结构的页面，验证解析复用与分类落库。
        """
        factory = make_db(tmp_path)
        special_url = "http://yuc.wiki/new/"
        special_page = (
            PAGE_HEADER
            + build_entry(1, cn="未上线甲", jp="UPCOMING A", broadcast="10/3周六深夜")
            + build_entry(2, cn="未上线乙", jp="UPCOMING B", broadcast="10/5深夜")
            + "</body></html>"
        )
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, special_url: special_page})
        summary = sync_yuc.run_sync(
            "unreleased",
            session_factory=factory,
            apply=True,
            fetch=fetch,
            season_url=special_url,
            season_title="未上线新番",
            category="unreleased",
        )
        assert summary.season_code == "unreleased"
        assert summary.added == 2

        db = factory()
        try:
            rows = db.query(AnimeSchedule).all()
            assert len(rows) == 2
            for row in rows:
                assert row.season_code == "unreleased"
                assert row.category == "unreleased"
                assert row.source_url == special_url

            # 季度档期列表只收 YYYYMM，不含特殊分类
            season_list = crud.list_schedule_seasons(db)
            assert season_list == []
            # 分类列表给出三项，未上线新番计数为 2，其余为 0
            cat_list = {c["code"]: c["count"] for c in crud.list_schedule_categories(db)}
            assert cat_list == {"unreleased": 2, "ova_sp": 0, "cn_movie": 0}
        finally:
            db.close()

    def test_source_change_updates_and_new_entry_added(self, tmp_path):
        """源数据变化时更新对应记录；新作品作为新增处理，其余不变。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})
        sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        fetch_v2 = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML_V2})
        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch_v2)

        assert (summary.added, summary.updated, summary.unchanged) == (1, 1, 2)

        db = factory()
        try:
            assert db.query(AnimeSchedule).count() == 4
            updated = (
                db.query(AnimeSchedule)
                .filter(AnimeSchedule.title_main == "顶点武装")
                .one()
            )
            assert updated.weekday == 7
            assert updated.broadcast_time == "23:00"
            assert updated.broadcast_note == "10/4周日 23:00 再放送"
            assert (
                db.query(AnimeSchedule)
                .filter(AnimeSchedule.title_main == "新加入的作品")
                .count()
                == 1
            )
        finally:
            db.close()

    def test_run_records_distinguish_modes(self, tmp_path):
        """先 dry-run 再 apply：两条运行记录模式与统计各自正确。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})
        sync_yuc.run_sync(None, session_factory=factory, fetch=fetch)
        sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        db = factory()
        try:
            runs = list(db.query(ScheduleSyncRun).order_by(ScheduleSyncRun.id))
            assert [r.mode for r in runs] == ["dry-run", "apply"]
            assert runs[0].added == 0 and runs[1].added == 3
            assert all(r.finished_at is not None for r in runs)
            assert runs[0].season_code == runs[1].season_code == "202610"
        finally:
            db.close()

    def test_unknown_season_raises_before_any_write(self, tmp_path):
        """指定不存在的档期：明确报错并列出可用档期；不产生任何运行记录。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

        with pytest.raises(sync_yuc.SeasonNotFoundError, match="199901"):
            sync_yuc.run_sync("199901", session_factory=factory, fetch=fetch)

        db = factory()
        try:
            assert db.query(ScheduleSyncRun).count() == 0
            assert db.query(AnimeSchedule).count() == 0
        finally:
            db.close()

    def test_page_fetch_failure_is_recorded(self, tmp_path):
        """档期页请求失败：计入失败、运行记录说明原因，不写作品数据。"""
        factory = make_db(tmp_path)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML})  # 档期页 URL 未提供 -> 404

        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        assert summary.failed == 1
        assert summary.pages_fetched == 0
        db = factory()
        try:
            assert db.query(AnimeSchedule).count() == 0
            run = db.query(ScheduleSyncRun).one()
            assert run.failed == 1
            assert "请求失败" in run.message
        finally:
            db.close()

    def test_db_error_per_record_is_counted(self, tmp_path, monkeypatch):
        """单条数据库写入失败：计入失败并回滚，不中断其余条目与运行记录。"""
        factory = make_db(tmp_path)

        def broken_upsert(db, data):
            raise OperationalError("INSERT", {}, OSError("disk I/O error"))

        monkeypatch.setattr(crud, "upsert_schedule", broken_upsert)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        assert summary.failed == 3
        assert summary.added == 0
        assert all("数据库写入失败" in o.reason for o in summary.outcomes if o.status == "failed")
        db = factory()
        try:
            run = db.query(ScheduleSyncRun).one()
            assert run.failed == 3
            assert "数据库写入失败" in run.message
        finally:
            db.close()

    def test_cross_language_duplicate_merged(self, tmp_path):
        """同作品一条中文主、一条日文主（title_jp 相同）：合并为一条并保留中文。

        复现线上 bug：yuc.wiki 档期页同一部作品被列两次，一次只有日文原名
        （title_cn 为空），一次带中文译名；两条 title_jp 相同、title_main 一中
        一日，旧逻辑按主标题去重挡不住而重复入库。修复后按日文名归组合并，并
        优先保留带中文译名的条目。
        """
        factory = make_db(tmp_path)
        # 在固定 4 条 fixture 之外，额外注入一对中日文重复（jp 相同、main 不同）。
        extra = (
            build_entry(5, cn="转生成哥布林有什么问题吗", jp="転生ゴブリンだけど質問ある？", broadcast="周一 25:00")
            + build_entry(6, cn=None, jp="転生ゴブリンだけど質問ある？", broadcast="周一 25:00")
        )
        html = build_season_page(extra_entry=extra)
        fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: html})

        summary = sync_yuc.run_sync(None, session_factory=factory, apply=True, fetch=fetch)

        # 固定 fixture 内 A01/A04 同主标题重复 -> 4 条解析合并为 3；
        # 额外一对中日文重复 -> 合并为 1；合计 4 条有效、2 条重复。
        assert summary.valid == 4
        assert summary.duplicates == 2
        assert summary.added == 4

        db = factory()
        try:
            rows = db.query(AnimeSchedule).all()
            goblin = [r for r in rows if r.title_jp == "転生ゴブリンだけど質問ある？"]
            assert len(goblin) == 1
            assert goblin[0].title_cn == "转生成哥布林有什么问题吗"
            assert goblin[0].title_main == "转生成哥布林有什么问题吗"
        finally:
            db.close()

    def test_atom_fetch_failure_propagates(self, tmp_path):
        """Atom 都拿不到：抛出明确网络错误，且不产生任何数据库写入。"""
        factory = make_db(tmp_path)

        def broken_fetch(url):
            raise sync_yuc.YucFetchError(f"请求 {url} 失败：请求超时")

        with pytest.raises(sync_yuc.YucFetchError, match="超时"):
            sync_yuc.run_sync(None, session_factory=factory, fetch=broken_fetch)

        db = factory()
        try:
            assert db.query(ScheduleSyncRun).count() == 0
        finally:
            db.close()


# ---------- 网络层 ----------


class TestFetchUrl:
    def test_retry_then_success(self):
        # bytes 步骤是正常响应；网络错误要用异常步骤模拟（OSError 可重试）
        opener = FakeOpener([OSError("boom"), OSError("boom"), "页面内容".encode("utf-8")])
        slept: list[float] = []

        body = sync_yuc.fetch_url(
            "https://yuc.wiki/202610/", opener=opener, retries=2, sleep=slept.append
        )

        assert body == "页面内容"
        assert len(opener.calls) == 3
        assert slept == [0.5, 1.0]

    def test_http_404_fails_fast(self):
        opener = FakeOpener(
            [urllib.error.HTTPError("https://yuc.wiki/x", 404, "Not Found", None, None)]
        )
        with pytest.raises(sync_yuc.YucFetchError, match="404"):
            sync_yuc.fetch_url("https://yuc.wiki/x", opener=opener, retries=3)
        assert len(opener.calls) == 1

    def test_timeout_retried_then_reported(self):
        opener = FakeOpener([TimeoutError("timed out"), TimeoutError("timed out")])
        with pytest.raises(sync_yuc.YucFetchError, match="超时"):
            sync_yuc.fetch_url("https://yuc.wiki/x", opener=opener, retries=1, sleep=lambda _: None)
        assert len(opener.calls) == 2

    def test_abnormal_body_does_not_crash(self):
        """非 UTF-8/乱码响应按 replace 解码为字符串，不抛异常。"""
        opener = FakeOpener([b"\xff\xfe garbage \x00bytes"])
        body = sync_yuc.fetch_url("https://yuc.wiki/x", opener=opener, retries=0)
        assert isinstance(body, str) and "garbage" in body

    def test_retryable_500_then_success(self):
        opener = FakeOpener(
            [
                urllib.error.HTTPError("https://yuc.wiki/x", 503, "Service Unavailable", None, None),
                "ok".encode("utf-8"),
            ]
        )
        body = sync_yuc.fetch_url("https://yuc.wiki/x", opener=opener, retries=1, sleep=lambda _: None)
        assert body == "ok"
        assert len(opener.calls) == 2


# ---------- 只读查询 API ----------


class TestScheduleApi:
    @pytest.fixture()
    def seeded(self, tmp_path):
        """临时库：202610 两条（周 6 / 无星期）、202607 一条，两条运行记录。"""
        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)  # 同步记录接口仅管理员可访问
        db = factory()
        try:
            upsert_sample(db, season="202610", source_key="银翼骑士", title_main="银翼骑士",
                          title_cn="银翼骑士", title_jp="銀翼のナイト", weekday=6,
                          work_type="漫画改编动画", work_tags="科幻/机战")
            upsert_sample(db, season="202610", source_key="无星期作品", title_main="无星期作品",
                          title_cn="无星期作品", title_jp=None, weekday=None,
                          broadcast_time=None, broadcast_note=None, image_url=None,
                          platforms=[])
            upsert_sample(db, season="202607", source_key="旧档期作品", title_main="旧档期作品",
                          title_cn="旧档期作品", weekday=1)
            dry = crud.create_sync_run(db, season_code="202610", mode="dry-run")
            crud.finish_sync_run(db, dry, summary={"valid": 3,
                                                   "message": "dry-run 预览：未写入任何新番数据"})
            apply_run = crud.create_sync_run(db, season_code="202610", mode="apply")
            crud.finish_sync_run(db, apply_run, summary={"valid": 3, "added": 3})
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

    def test_list_pagination(self, seeded):
        resp = seeded.get("/api/schedule", params={"season": "202610", "page_size": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1 and body["page_size"] == 1
        assert body["total_pages"] == 2
        assert len(body["items"]) == 1

        page2 = seeded.get("/api/schedule", params={"season": "202610", "page": 2, "page_size": 1})
        assert page2.json()["items"][0]["title_main"] != body["items"][0]["title_main"]

    def test_list_filters_and_search(self, seeded):
        by_weekday = seeded.get("/api/schedule", params={"season": "202610", "weekday": 6})
        items = by_weekday.json()["items"]
        assert [i["title_main"] for i in items] == ["银翼骑士"]

        by_keyword = seeded.get("/api/schedule", params={"q": "銀翼"})
        assert [i["title_main"] for i in by_keyword.json()["items"]] == ["银翼骑士"]

        assert seeded.get("/api/schedule", params={"weekday": 9}).status_code == 400

    def test_list_without_season_returns_all(self, seeded):
        body = seeded.get("/api/schedule").json()
        assert body["total"] == 3

    def test_seasons_endpoint(self, seeded):
        body = seeded.get("/api/schedule/seasons").json()
        assert body["total"] == 2
        assert [s["season_code"] for s in body["seasons"]] == ["202610", "202607"]
        assert body["seasons"][0]["count"] == 2
        assert body["seasons"][0]["last_synced_at"]

    def test_categories_endpoint(self, seeded):
        """特殊分类接口：固定三项 + 各自作品数；未同步的分类 count=0。"""
        body = seeded.get("/api/schedule/categories").json()
        assert body["total"] == 3
        assert [c["code"] for c in body["categories"]] == [
            "unreleased", "ova_sp", "cn_movie",
        ]
        assert [c["label"] for c in body["categories"]] == [
            "未上线新番", "剧场版ova与特别篇", "大陆引进剧场版",
        ]
        assert all(c["count"] == 0 for c in body["categories"])

    def test_schedule_api_category_filter(self, tmp_path):
        """按 category 过滤：只返回该分类作品，且与季度档期互不串味。"""
        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)
        db = factory()
        try:
            upsert_sample(db, season="202610", source_key="季度作品", title_main="季度作品",
                          title_cn="季度作品", title_jp=None)
            upsert_sample(db, season="unreleased", source_key="未上线作品", title_main="未上线作品",
                          title_cn="未上线作品", title_jp=None, category="unreleased")
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
        try:
            resp = client.get("/api/schedule", params={"category": "unreleased"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 1
            assert body["items"][0]["title_main"] == "未上线作品"
            assert body["items"][0]["category"] == "unreleased"

            # 非法分类码 400（不静默返回全部）
            bad = client.get("/api/schedule", params={"category": "不存在的分类"})
            assert bad.status_code == 400

            # 分类视图不串入季度档期作品
            assert client.get("/api/schedule", params={"season": "202610"}).json()["total"] == 1
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_detail_and_404(self, seeded):
        listing = seeded.get("/api/schedule", params={"season": "202610"}).json()
        target = listing["items"][0]
        detail = seeded.get(f"/api/schedule/{target['id']}")
        assert detail.status_code == 200
        assert detail.json()["title_main"] == target["title_main"]
        # 可选字段缺失的旧记录仍返回完整键结构
        assert set(detail.json()) >= {
            "id", "season_code", "title_main", "title_cn", "title_jp", "weekday",
            "broadcast_time", "broadcast_note", "image_url", "platforms",
            "source_url", "last_synced_at",
        }
        missing = seeded.get("/api/schedule/99999")
        assert missing.status_code == 404

    def test_runs_endpoint(self, seeded):
        body = seeded.get("/api/schedule/runs").json()
        assert body["total"] == 2
        assert [r["mode"] for r in body["runs"]] == ["apply", "dry-run"]  # 最新在前
        assert body["runs"][0]["added"] == 3
        assert body["runs"][1]["added"] == 0
        assert "dry-run" in body["runs"][1]["message"]


# ---------- 页面 ----------


class TestSchedulePages:
    @pytest.fixture()
    def seeded(self, tmp_path):
        """临时库：两个档期各一条 + 一条 dry-run 运行记录。"""
        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)  # 同步历史页面仅管理员可访问
        db = factory()
        try:
            upsert_sample(db, season="202610", source_key="银翼骑士", title_main="银翼骑士",
                          title_cn="银翼骑士", title_jp="銀翼のナイト", weekday=6,
                          work_type="漫画改编动画", work_tags="科幻/机战")
            upsert_sample(db, season="202607", source_key="旧档期作品", title_main="旧档期作品",
                          title_cn="旧档期作品", weekday=1)
            run = crud.create_sync_run(db, season_code="202610", mode="dry-run")
            crud.finish_sync_run(db, run, summary={"valid": 1, "message": "dry-run 预览"})
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

    def test_schedule_page_renders_works(self, seeded):
        resp = seeded.get("/schedule")
        assert resp.status_code == 200
        assert "银翼骑士" in resp.text
        assert "銀翼のナイト" in resp.text
        assert "周六" in resp.text
        assert 'referrerpolicy="no-referrer"' in resp.text  # 热链图片需要该属性
        assert "https://www.bilibili.com/" in resp.text
        # 去显著化（2026-10-18）：卡片不再外链 Yuc.wiki，封面点击落到站内搜索
        assert "Yuc" not in resp.text
        assert "/contents?q=" in resp.text
        assert "/manage/sync/runs" in resp.text  # 导航入口

    def test_schedule_page_season_switch_and_filter(self, seeded):
        old = seeded.get("/schedule", params={"season": "202607"})
        assert old.status_code == 200
        assert "旧档期作品" in old.text
        assert "银翼骑士" not in old.text

        search = seeded.get("/schedule", params={"season": "202610", "q": "银翼"})
        assert "银翼骑士" in search.text
        assert "样例作品" not in search.text

    def test_schedule_page_weekday_filter_and_empty_value(self, seeded):
        """星期筛选可用；空值/非数字宽容忽略不 422（模板「全部」与分页链接会发 weekday=）。"""
        by_weekday = seeded.get("/schedule", params={"season": "202610", "weekday": 6})
        assert by_weekday.status_code == 200
        assert "银翼骑士" in by_weekday.text
        assert "无星期作品" not in by_weekday.text

        for bad in ("", "abc", "9"):
            resp = seeded.get("/schedule", params={"season": "202610", "weekday": bad})
            assert resp.status_code == 200, f"weekday={bad!r} 应宽容忽略"
            assert "银翼骑士" in resp.text  # 回落到无筛选分组视图

    def test_schedule_pagination_link_encodes_q(self, tmp_path):
        """分页链接的 q/season 必须 URL 编码：q 含 & 不断链（回归 2026-10-19）。"""
        import html
        import re

        from app.models import AnimeSchedule

        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)
        db = factory()
        try:
            for i in range(1, 22):  # 21 条 > SCHEDULE_PAGE_SIZE(20)，触发翻页
                upsert_sample(
                    db,
                    source_key=f"A&B作品{i:02d}",
                    title_main=f"A&B作品{i:02d}",
                    title_cn=f"A&B作品{i:02d}",
                    title_jp=None,
                    weekday=6,
                    image_url=None,
                    platforms=[],
                )
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
        try:
            resp = client.get(
                "/schedule", params={"season": "202610", "q": "A&B", "weekday": 6}
            )
            assert resp.status_code == 200
            # q 已编码（A%26B），链接里不再出现裸的 A&B；翻页后仍能命中
            assert "q=A%26B" in resp.text
            assert "q=A&B" not in resp.text
            match = re.search(r'href="(/schedule\?[^"]*page=2)"', resp.text)
            assert match, "应存在第二页链接"
            # HTML 属性里的 & 被转义为 &amp;（正确行为），请求前先还原成浏览器语义
            page2 = client.get(html.unescape(match.group(1)))
            assert page2.status_code == 200
            assert "第 2" in page2.text  # 真的翻到了第二页
            # Jinja 自动转义：标题里的 & 渲染为 &amp;
            assert "A&amp;B作品" in page2.text  # 翻页后筛选条件仍然生效
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_schedule_page_empty_database(self, tmp_path):
        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)  # 同步历史页面仅管理员可访问

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        client = TestClient(app)
        app.dependency_overrides[get_db] = override_get_db
        login_as_admin(client)
        try:
            resp = client.get("/schedule")
            assert resp.status_code == 200
            # 空态面向访客：不再渲染同步脚本命令行（去显著化）
            assert "整理中" in resp.text
            assert "sync_yuc_schedule" not in resp.text
            runs = client.get("/manage/sync/runs")
            assert runs.status_code == 200
            assert "暂无同步记录" in runs.text
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_schedule_page_shows_work_type_and_tags(self, seeded):
        """作品类型与题材标签在档期页与 API 都能读到。"""
        body = seeded.get("/api/schedule", params={"season": "202610"}).json()
        item = next(i for i in body["items"] if i["title_main"] == "银翼骑士")
        assert item["work_type"] == "漫画改编动画"
        assert item["work_tags"] == "科幻/机战"

        page = seeded.get("/schedule", params={"season": "202610"})
        assert page.status_code == 200
        assert "漫画改编动画" in page.text
        assert "科幻/机战" in page.text

    def test_runs_page_lists_history(self, seeded):
        resp = seeded.get("/manage/sync/runs")
        assert resp.status_code == 200
        assert "202610" in resp.text
        assert "dry-run" in resp.text
        assert "badge" in resp.text  # 运行模式以徽标展示

    def test_schedule_page_category_view(self, tmp_path):
        """特殊分类视图：只显示该分类作品，不混季度档期作品；季度 tab 也不含分类作品。"""
        factory = make_db(tmp_path)
        create_accounts(factory, admin=True, user=False)
        db = factory()
        try:
            # 一条季度档期 + 三条特殊分类作品
            upsert_sample(db, season="202610", source_key="季度作品", title_main="季度作品",
                          title_cn="季度作品", title_jp=None)
            for code, title in (
                ("unreleased", "未上线作品甲"),
                ("ova_sp", "特篇作品乙"),
                ("cn_movie", "引进剧场丙"),
            ):
                upsert_sample(db, season=code, source_key=title, title_main=title,
                              title_cn=title, title_jp=None, category=code)
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
        try:
            # 1) 默认（季度视图）：出现季度作品，不出现任何特殊分类作品
            home = client.get("/schedule")
            assert home.status_code == 200
            assert "季度作品" in home.text
            assert "未上线作品甲" not in home.text
            assert "特篇作品乙" not in home.text
            assert "引进剧场丙" not in home.text
            # 三个分类 tab 都在，且带计数
            for label in ("未上线新番", "剧场版ova与特别篇", "大陆引进剧场版"):
                assert label in home.text
            assert "分类 未上线新番" not in home.text  # 默认不是分类视图

            # 2) 分类视图：只显示该分类作品
            resp = client.get("/schedule", params={"category": "unreleased"})
            assert resp.status_code == 200
            assert "分类 未上线新番" in resp.text
            assert "未上线作品甲" in resp.text
            assert "特篇作品乙" not in resp.text
            assert "引进剧场丙" not in resp.text
            assert "季度作品" not in resp.text
            # 分类视图下隐藏季度筛选框，改为隐藏的 category 维度
            assert 'name="category" value="unreleased"' in resp.text

            # 3) 其余两个分类同理
            for code, title in (("ova_sp", "特篇作品乙"), ("cn_movie", "引进剧场丙")):
                other = client.get("/schedule", params={"category": code})
                assert other.status_code == 200
                assert title in other.text
                assert "未上线作品甲" not in other.text
                assert "季度作品" not in other.text

            # 4) 分类视图下关键词筛选仍然生效
            hit = client.get("/schedule", params={"category": "unreleased", "q": "甲"})
            assert hit.status_code == 200
            assert "未上线作品甲" in hit.text
            miss = client.get("/schedule", params={"category": "unreleased", "q": "乙"})
            assert miss.status_code == 200
            assert "未上线作品甲" not in miss.text

            # 5) 非法 category 回落到季度视图（不 500、不空页）
            bad = client.get("/schedule", params={"category": "不存在的分类"})
            assert bad.status_code == 200
            assert "季度作品" in bad.text
            assert "分类 " not in bad.text
        finally:
            app.dependency_overrides.pop(get_db, None)


# ---------- 模型注册与约束 ----------


class TestScheduleModels:
    def test_tables_created_with_unique_constraint(self, tmp_path):
        engine = build_engine(f"sqlite:///{tmp_path / 'schema.db'}")
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"anime_schedules", "schedule_sync_runs"} <= tables

        constraints = inspector.get_unique_constraints("anime_schedules")
        names = {c.get("name") for c in constraints}
        assert "uq_anime_schedules_season_key" in names

    def test_as_dict_shapes_are_stable(self, tmp_path):
        factory = make_db(tmp_path)
        db = factory()
        try:
            record, _ = upsert_sample(db)
            item = record.as_dict()
            assert item["platforms"] == [{"name": "bilibili", "url": "https://www.bilibili.com/"}]
            assert set(item) == {
                "id", "season_code", "source_key", "category", "title_main", "title_cn",
                "title_jp", "weekday", "broadcast_time", "broadcast_note", "work_type",
                "work_tags", "image_url", "platforms", "source_url", "last_synced_at",
            }
        finally:
            db.close()


# ---------- 批量同步与 TLS 降级（放量回填历史档期） ----------


def test_run_sync_seasons_writes_one_summary_per_season(tmp_path):
    """批量同步：每个档期一份汇总，同一页面内容也按档期分别入库。"""
    factory = make_db(tmp_path)
    fetch = make_stub_fetch({
        ATOM_URL: ATOM_XML,
        SEASON_URL: SEASON_HTML,
        "http://yuc.wiki/202607/": SEASON_HTML,
    })

    summaries = sync_yuc.run_sync_seasons(
        ["202610", "202607"], session_factory=factory, apply=True, fetch=fetch
    )

    assert [s.season_code for s in summaries] == ["202610", "202607"]
    assert [s.added for s in summaries] == [3, 3]


def test_run_sync_seasons_continues_after_page_failure(tmp_path):
    """某个档期页抓不到只让该档期失败，不影响其它档期同步。"""
    factory = make_db(tmp_path)
    fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

    summaries = sync_yuc.run_sync_seasons(
        ["202610", "202607"], session_factory=factory, apply=True, fetch=fetch
    )

    assert summaries[0].added == 3
    assert summaries[1].failed == 1
    assert summaries[1].outcomes[0].status == "failed"


def test_run_sync_seasons_records_missing_season(tmp_path):
    """档期不在 Atom 中时记为该档期失败，而不是抛异常中断整体。"""
    factory = make_db(tmp_path)
    fetch = make_stub_fetch({ATOM_URL: ATOM_XML, SEASON_URL: SEASON_HTML})

    summaries = sync_yuc.run_sync_seasons(
        ["202610", "209912"], session_factory=factory, apply=True, fetch=fetch
    )

    assert summaries[0].added == 3
    assert summaries[1].season_code == "209912"
    assert summaries[1].failed == 1


def test_build_opener_insecure_skips_certificate_checks():
    """--insecure 给 opener 装上不校验证书的 HTTPSHandler（源站证书过期时用）。"""
    opener = sync_yuc.build_opener(None, insecure=True)
    handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert handlers, "insecure=True 时应安装 HTTPSHandler"
    assert handlers[0]._context.check_hostname is False

    strict = sync_yuc.build_opener(None)
    strict_handlers = [h for h in strict.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert strict_handlers and strict_handlers[0]._context.check_hostname is True


def test_cli_rejects_season_and_seasons_together():
    """--season 与 --seasons 语义互斥，同时给是参数错误。"""
    assert sync_yuc.main(["--season", "202610", "--seasons", "2"]) == 2


def test_cli_rejects_category_with_season():
    """--category 与 --season 互斥。"""
    assert sync_yuc.main(["--category", "unreleased", "--season", "202610"]) == 2


def test_cli_rejects_unknown_category_code():
    """未知分类码直接报参数错误，不触网。"""
    assert sync_yuc.main(["--category", "not_a_category"]) == 2


def test_cli_special_sync_does_not_crash(monkeypatch, tmp_path):
    """CLI 走特殊分类分支时不因限频变量名写错而崩（回归 2026-10-21）。

    之前分支里用的是裸 delay（应为 args.delay），--special 会 NameError。
    """
    calls: list[str] = []

    def fake_fetch(url):
        calls.append(url)
        if url.endswith("/atom.xml"):
            return ATOM_XML
        return SEASON_HTML

    factory = make_db(tmp_path, name="cli.db")

    class _StubEngine:
        def dispose(self):
            pass

    monkeypatch.setattr(sync_yuc, "_make_fetcher", lambda **kwargs: fake_fetch)
    monkeypatch.setattr(
        sync_yuc, "build_session_factory", lambda url, busy_timeout=30.0: (factory, _StubEngine())
    )
    monkeypatch.setattr(sync_yuc.time, "sleep", lambda _s: None)

    # 只验证「能跑通到同步阶段」：用 --category 单分类，避免跑三遍
    exit_code = sync_yuc.main(
        ["--category", "unreleased", "--database-url", f"sqlite:///{tmp_path / 'cli.db'}"]
    )
    assert exit_code == 0
    assert any(url.endswith("/atom.xml") for url in calls)
    assert "http://yuc.wiki/new/" in calls


def test_resolve_insecure_prefers_flag_over_env():
    """显式 --insecure 优先于环境变量；环境变量可让 cron/管理端免改命令行。"""
    assert sync_yuc.resolve_insecure(True, env={"YUC_INSECURE": "0"}) is True
    assert sync_yuc.resolve_insecure(False, env={"YUC_INSECURE": "1"}) is False
    assert sync_yuc.resolve_insecure(None, env={"YUC_INSECURE": "1"}) is True
    assert sync_yuc.resolve_insecure(None, env={"YUC_INSECURE": "true"}) is True
    assert sync_yuc.resolve_insecure(None, env={}) is False


def test_run_sync_accepts_insecure_none_without_env(monkeypatch):
    """insecure=None 且无环境变量时按严格校验构造请求器（不读真实网络）。"""
    monkeypatch.delenv("YUC_INSECURE", raising=False)
    seen = {}

    def fake_make_fetcher(**kwargs):
        seen.update(kwargs)
        raise AssertionError("真实请求器不该在这里被调用")

    monkeypatch.setattr(sync_yuc, "_make_fetcher", fake_make_fetcher)
    monkeypatch.setattr(sync_yuc, "build_opener", lambda proxy, insecure: ("opener", insecure))
    with pytest.raises(AssertionError):
        sync_yuc.run_sync(None, session_factory=lambda: None, fetch=None, insecure=None)
    assert seen["opener"] == ("opener", False)
