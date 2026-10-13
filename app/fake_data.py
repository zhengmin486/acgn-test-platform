"""开发/测试用种子数据。

第二阶段起由 app/crud.seed_if_empty 在内容表为空时播种进数据库；
本列表仅作为种子来源，便于联调、页面展示和自动化测试复用。
"""

FAKE_CONTENTS: list[dict] = [
    {
        "id": 1,
        "title": "星海航线",
        "type": "anime",
        "summary": "一部关于星际开拓的原创动画，讲述少年船员在未知星域寻找家园的故事。",
        "rating": 8.7,
    },
    {
        "id": 2,
        "title": "漫画屋的日常",
        "type": "comic",
        "summary": "记录一家街角漫画店四季更替的治愈系连载漫画。",
        "rating": 8.2,
    },
    {
        "id": 3,
        "title": "迷宫回响",
        "type": "game",
        "summary": "以随机迷宫与声音解谜为核心的独立冒险游戏。",
        "rating": 9.1,
    },
    {
        "id": 4,
        "title": "雨夜轻小说精选",
        "type": "novel",
        "summary": "收录多篇以雨夜为背景的青春奇幻短篇小说。",
        "rating": 7.9,
    },
    {
        "id": 5,
        "title": "Cyber Shrine",
        "type": "game",
        "summary": "赛博朋克风格的和风动作游戏，玩家扮演守护电子神社的巫女。",
        "rating": 8.5,
    },
    {
        "id": 6,
        "title": "薄荷色季节",
        "type": "anime",
        "summary": "四名少女在滨海小镇组成乐队的日常向动画。",
        "rating": 8.0,
    },
]
