"""界面域边界测试：防止「内容平台」与「测试平台」两套界面再次混在一起。

三条硬边界（静态检查，不依赖数据库，秒级完成）：

1. 布局归属：测试域页面只能继承 base_admin.html，内容域页面只能继承 base_content.html；
2. 依赖方向：内容管理路由模块不得 import 测试执行相关模块，反之亦然；
3. 导航归属：测试平台布局里不出现内容管理链接，内容平台布局里不出现测试页面链接。

新增页面时如果放错布局或写错 import，这里会先失败，而不是等到界面看起来又混了才发现。
"""

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEMPLATES = APP_DIR / "templates"

# 测试域页面（开发者工具）——只允许这几个模板继承测试平台布局
TEST_DOMAIN_TEMPLATES = {
    "testcases.html",
    "case_edit.html",
    "runs.html",
    "run_detail.html",
    "batches.html",
    "batch_detail.html",
    "case_generate.html",
}


def _extends_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines()[:5]:
        if '{% extends "' in line:
            return line.split('"')[1]
    return ""


def test_test_domain_templates_use_admin_layout_only():
    """只有测试域页面继承 base_admin.html，且测试域页面一个都不少。"""
    actual = {
        path.name
        for path in TEMPLATES.glob("*.html")
        if _extends_of(path) == "base_admin.html"
    }
    assert actual == TEST_DOMAIN_TEMPLATES


def test_content_domain_templates_use_content_layout():
    """内容运营 / 站点治理页面必须落在内容平台布局里（不再套测试平台的壳）。"""
    content_domain = sorted(TEMPLATES.glob("admin_*.html")) + [
        TEMPLATES / "schedule_runs.html",
    ]
    for path in content_domain:
        assert _extends_of(path) == "base_content.html", f"{path.name} 应继承 base_content.html"


def _imported_modules(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_content_admin_module_does_not_import_test_runner():
    """内容管理路由不依赖测试执行逻辑（共享的只有数据层与 admin_common）。"""
    forbidden = {"app.testing_pages", "app.runner", "app.cases", "app.site_admin_pages"}
    for name in ("content_admin_pages.py", "admin_common.py"):
        imported = _imported_modules(APP_DIR / name)
        assert not (imported & forbidden), f"{name} 不应 import {imported & forbidden}"


def test_site_admin_module_does_not_import_test_runner():
    """站点设置路由同样不依赖测试平台与内容管理路由。"""
    imported = _imported_modules(APP_DIR / "site_admin_pages.py")
    forbidden = {"app.testing_pages", "app.runner", "app.content_admin_pages", "app.cases"}
    assert not (imported & forbidden), f"site_admin_pages.py 不应 import {imported & forbidden}"


def test_testing_module_does_not_import_admin_pages():
    """测试平台路由不依赖内容管理 / 站点设置路由（两域只共享 crud / page_common）。"""
    imported = _imported_modules(APP_DIR / "testing_pages.py")
    forbidden = {"app.content_admin_pages", "app.site_admin_pages",
                 "app.business_pages", "app.submission_pages"}
    assert not (imported & forbidden), f"testing_pages.py 不应 import {imported & forbidden}"


def test_business_pages_does_not_own_admin_routes():
    """对读者开放的内容页不再承载管理路由（同步历史已迁到 /manage/sync/runs）。"""
    source = (APP_DIR / "business_pages.py").read_text(encoding="utf-8")
    routes = [line.strip() for line in source.splitlines() if line.strip().startswith("@router.")]
    offenders = [line for line in routes if "/manage" in line or "/admin" in line]
    assert not offenders, f"business_pages.py 不应注册管理路由：{offenders}"


def test_admin_layout_has_no_content_management_links():
    """测试平台布局源码里不出现内容管理 / 站点治理入口。"""
    source = (TEMPLATES / "base_admin.html").read_text(encoding="utf-8")
    for href in ("/manage", "/admin/accounts", "/admin/invites", "/admin/roles"):
        assert href not in source, f"base_admin.html 不应出现 {href}"


def test_content_layout_has_no_test_links_except_entry_button():
    """内容平台布局只保留一个跨域入口按钮（测试管理），不列测试页面导航项。"""
    source = (TEMPLATES / "base_content.html").read_text(encoding="utf-8")
    for href in ('"/runs"', '"/batches"'):
        assert href not in source, f"base_content.html 不应出现测试页面导航 {href}"
