"""批量导入官方来源 ACGN 资讯。默认预览，--apply 才写入。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

# Allow execution as `python scripts\import_articles.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database import SessionLocal
from app.models import Article

# Summaries are concise Chinese summaries of the linked official announcements.
ARTICLES = [
    {
        "title": "《BLEACH 千年血战篇》与 Graniph 首次联名，推出 17 款商品",
        "category": "动漫资讯",
        "summary": "Aniplex 官方宣布《BLEACH 千年血战篇》与 Graniph 推出首次联名系列，共计 17 款商品。",
        "body": "Aniplex 于 2026 年 10 月 14 日发布公告，宣布 TV 动画《BLEACH 千年血战篇》与服饰品牌 Graniph 首次合作，推出共 17 款联名商品。商品设计与销售安排请以官方公告及合作方页面为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71436",
        "published_at": datetime(2026, 10, 14),
    },
    {
        "title": "《乐园追放 心之共鸣》公布主题曲《Beyond the Night》",
        "category": "动漫资讯",
        "summary": "Aniplex 公布剧场动画《乐园追放 心之共鸣》的主题曲《Beyond the Night》相关信息。",
        "body": "Aniplex 于 2026 年 10 月 13 日公布剧场动画《乐园追放 心之共鸣》的主题曲《Beyond the Night》相关信息。歌曲发行及后续安排以官方公告为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71476",
        "published_at": datetime(2026, 10, 13),
    },
    {
        "title": "动画《ブレス》公开中野明治角色 PV 与声优信息",
        "category": "动漫资讯",
        "summary": "官方公开动画《ブレス》中野明治的角色 PV，并宣布该角色由内山昂辉配音。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 15 日发布公告，公开动画《ブレス》中野明治的角色 PV 与配音信息。中野明治一角由内山昂辉出演，角色 PV 首次公开该角色的声音。更多角色信息与影像请查看官方原文。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71486",
        "published_at": datetime(2026, 10, 15),
    },
    {
        "title": "《バーテックスフォース》公开第三话先行画面与剧情梗概",
        "category": "动漫资讯",
        "summary": "官方发布动画《バーテックスフォース》第三话先行画面与剧情梗概。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 15 日公布动画《バーテックスフォース》第三话先行画面与剧情梗概，并提供作品官方分集页面。具体内容与播出信息请以官方公告为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71485",
        "published_at": datetime(2026, 10, 15),
    },
    {
        "title": "《GROTESQQQUE-グロテスク-》公布与卡拉 OK 铁人的联名活动",
        "category": "活动资讯",
        "summary": "Aniplex 公布《GROTESQQQUE-グロテスク-》将与日本卡拉 OK 品牌卡拉 OK 铁人开展联名活动。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 13 日公布《GROTESQQQUE-グロテスク-》与日本卡拉 OK 品牌卡拉 OK 铁人的联名活动。举办时间、参与门店与活动细节请以官方原文及后续公告为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71475",
        "published_at": datetime(2026, 10, 13),
    },
    {
        "title": "《魔法少女小圆〈瓦尔普吉斯的回天〉》公布第八周观影特典",
        "category": "动漫资讯",
        "summary": "剧场版《魔法少女小圆〈瓦尔普吉斯的回天〉》公布第八周观影特典安排。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 13 日发布公告，公布剧场版《魔法少女小圆〈瓦尔普吉斯的回天〉》第八周观影特典安排。特典领取日期、影院范围及库存情况请以官方公告和参与影院通知为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71474",
        "published_at": datetime(2026, 10, 13),
    },
    {
        "title": "《乐园追放 心之共鸣》公开最新主视觉图",
        "category": "动漫资讯",
        "summary": "《乐园追放 心之共鸣》公开最新主视觉图，画面由角色设计师齋藤将嗣绘制，呈现五名主要角色与主角机体阿哈恩。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 12 日发布公告，公开《乐园追放 心之共鸣》的最新主视觉图。官方介绍称，视觉图由角色设计师齋藤将嗣绘制，五名主要角色与主角机体阿哈恩一同出现在雪山场景中。更多作品信息请查看官方原文。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71470",
        "published_at": datetime(2026, 10, 12),
    },
    {
        "title": "《GROTESQQQUE-グロテスク-》剧中曲《TERASE》开启先行发行",
        "category": "音乐资讯",
        "summary": "官方宣布《GROTESQQQUE-グロテスク-》相关剧中曲《TERASE》开始先行发行，并公布合作影像安排。",
        "body": "ANIPLEX NEWS BOX 于 2026 年 10 月 14 日发布公告，宣布《GROTESQQQUE-グロテスク-》相关剧中曲《TERASE》开始先行发行，并介绍合作影像安排。发行平台及后续活动详情请以官方公告为准。本文为官方公告的简要中文摘要。",
        "source": "ANIPLEX NEWS BOX",
        "original_url": "https://news.aniplex.co.jp/detail.html?id=71468",
        "published_at": datetime(2026, 10, 14),
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="导入官方来源 ACGN 资讯；默认预览，--apply 才写入。")
    parser.add_argument("--apply", action="store_true", help="正式写入数据库")
    args = parser.parse_args()

    print(f"数据库：{settings.database_url}")
    with SessionLocal() as db:
        urls = [item["original_url"] for item in ARTICLES]
        titles = [item["title"] for item in ARTICLES]
        existing_urls = set(db.scalars(select(Article.original_url).where(Article.original_url.in_(urls))).all())
        existing_titles = set(db.scalars(select(Article.title).where(Article.title.in_(titles))).all())
        pending = [item for item in ARTICLES if item["original_url"] not in existing_urls and item["title"] not in existing_titles]
        print(f"准备条目：{len(ARTICLES)}；已存在跳过：{len(ARTICLES)-len(pending)}；待新增：{len(pending)}")
        for i, item in enumerate(pending, 1):
            print(f"{i}. [{item['category']}] {item['title']}（{item['published_at']:%Y-%m-%d}）")
            print(f"   {item['original_url']}")
        if not args.apply:
            print("模式：dry-run，未写入数据库。确认后追加 --apply 正式导入。")
            return 0
        for item in pending:
            db.add(Article(**item, is_published=True))
        db.commit()
        print(f"正式导入完成：新增 {len(pending)} 条，跳过已存在 {len(ARTICLES)-len(pending)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
