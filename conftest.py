"""pytest 根配置：让 pytest 把项目根目录加入 sys.path，保证 "from app..." 可导入。"""


import pytest  # noqa: E402

from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_bangumi_meta_on_demand(monkeypatch):
    """测试一律不联网：详情页的「按需补全」在测试里关掉。

    需要验证补全逻辑的用例自行 monkeypatch 或直接调用 sync_content_meta(payload=...)。
    """
    monkeypatch.setattr(settings, "bangumi_meta_on_demand", False)
