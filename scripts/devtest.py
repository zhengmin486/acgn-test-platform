"""按改动选跑测试：把「只跑相关文件」的纪律变成一条命令。

日常开发铁律（runtime.md）：**严禁全量，只跑相关文件**（全量约 8 分钟）。
本工具把「改动 → 该跑哪些测试」机械化，省掉每次人工猜文件的成本：

1. 取改动文件：默认 ``git status --porcelain``（含未跟踪新文件），也可显式传参；
2. 静态解析 ``tests/*.py``（含 helpers）的 import，建立「测试文件 → app/scripts 模块」映射；
3. app 模块反向传播一层：改了 ``app/crud.py``，import crud 的页面模块的测试也选上；
4. 例外表：``conftest.py`` → 提示全量（影响所有用例的隔离性）；
   ``app/templates/``、``app/static/`` → UI 测试集；``docs/``、``.workbuddy/`` 等 → 不跑；
   拿不准 mappings 的文件 → 明确警告，建议 ``--all``，绝不静默漏跑。

用法：
    python scripts/devtest.py                # 按当前 git 改动选跑
    python scripts/devtest.py app/crud.py    # 显式指定改动文件（可多个）
    python scripts/devtest.py --collect      # 只列清单+原因，不执行
    python scripts/devtest.py --all          # 明确全量（约 8 分钟，慎用）
    python scripts/devtest.py --full-tb      # 完整 traceback（默认紧凑输出，省 token）

退出码继承 pytest。服务器上没有 .git 时，请显式传文件参数。
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

# 模板 / 样式改动 → 这些测试文件（页面渲染与布局归属的守门人）。
UI_TESTS = (
    "tests/test_ui_boundaries.py",
    "tests/test_ui_landing.py",
    "tests/test_admin_data.py",
    "tests/test_contents_browse.py",
    "tests/test_space.py",
    "tests/test_persona.py",
    "tests/test_home_posters.py",
    "tests/test_article_images.py",
)

# 改了也不影响任何测试的前缀。
IGNORED_PREFIXES = ("docs/", ".workbuddy/", "reports/", "backups/", "output/")

# 改了影响面覆盖全部用例的文件。
GLOBAL_FILES = {"conftest.py"}


# ---------- 静态 import 解析 ----------

def _parse_imports(path: Path) -> set[str]:
    """文件里 import 的模块名集合（只取 level=0 的绝对 import）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _resolve(name: str) -> str | None:
    """模块名 → 项目内文件相对路径；不在项目内（第三方库）返回 None。"""
    parts = name.split(".")
    if parts[0] in ("app", "scripts"):
        rel = "/".join(parts)
        for candidate in (f"{rel}.py", f"{rel}/__init__.py"):
            if (ROOT / candidate).is_file():
                return candidate
        return None
    if len(parts) == 1 and (TESTS_DIR / f"{parts[0]}.py").is_file():
        # tests/ 由 pytest prepend 进 sys.path，helper 用裸名 import
        return f"tests/{parts[0]}.py"
    return None


def _build_maps() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(测试文件 → import 的项目文件, app 模块 → import 的 app 模块)。

    只收 tests/test_*.py：helper（auth_helpers 等）不是 pytest 收集目标，
    它们的改动通过「谁 import 它」反查，不直接进清单。
    """
    test_imports: dict[str, set[str]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        resolved = {r for r in (_resolve(n) for n in _parse_imports(path)) if r}
        test_imports[f"tests/{path.name}"] = resolved

    app_imports: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").glob("*.py")):
        resolved = {r for r in (_resolve(n) for n in _parse_imports(path)) if r}
        app_imports[f"app/{path.name}"] = {m for m in resolved if m.startswith("app/")}
    return test_imports, app_imports


def _dependents(app_imports: dict[str, set[str]]) -> dict[str, set[str]]:
    """反向依赖一层：app/crud.py → {import 了 crud 的 app 模块}。

    app/main.py 是装配点（几乎所有测试都 from app.main import app），
    不允许它当传导点——否则改任何页面模块都会经由 main.py 引爆到全部测试。
    main.py 自身被改时走直接规则（选全部 import 它的测试），不走这里。
    """
    reverse: dict[str, set[str]] = {}
    for module, imported in app_imports.items():
        if module == "app/main.py":
            continue
        for target in imported:
            reverse.setdefault(target, set()).add(module)
    return reverse


def _route_literals(path: Path) -> set[str]:
    """页面模块里 ``@router.get("/x")`` 声明的路径字面量 + 首段前缀。

    页面测试是 URL 驱动的（TestClient + 路径字符串，不 import 页面模块），
    import 映射抓不到它们；用路由字面量反查：改了哪个页面模块，就选
    「源码里出现过它路由」的测试。宁可多选，不可漏选。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            arg = decorator.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            value = arg.value
            if not value.startswith("/") or value == "/":
                continue  # 裸 "/" 是任何路径的子串，没有区分度
            literals.add(value)
            segments = [s for s in value.split("/") if s and not s.startswith("{")]
            if segments:
                literals.add("/" + segments[0])
    return literals


def _test_sources() -> dict[str, str]:
    """测试文件源码缓存（路由字面量反查用；只收 pytest 收集目标）。"""
    sources: dict[str, str] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            sources[f"tests/{path.name}"] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return sources


# ---------- 改动收集 ----------

def _changed_files() -> list[str]:
    """git status --porcelain：改动的跟踪文件 + 未跟踪新文件（rename 取新路径）。"""
    result = subprocess.run(
        ["git", "status", "--porcelain"], check=False, cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        if len(line) <= 3:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:  # rename 形式：R  old.py -> new.py
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


# ---------- 选跑决策 ----------

def select(changed: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    """改动文件 → (选中的测试文件, 文件→原因, 警告列表)。"""
    test_imports, app_imports = _build_maps()
    dependents = _dependents(app_imports)
    sources = _test_sources()

    selected: dict[str, str] = {}   # 测试文件 → 原因
    warnings: list[str] = []
    relevant: set[str] = set()      # 改动会波及的项目文件（含反向传播）

    for path in changed:
        if path in GLOBAL_FILES:
            # conftest 改动影响所有用例的隔离性：选全量并明说，不靠用户自觉
            for test in sorted(test_imports):
                selected[test] = f"{path} 是全局夹具，影响所有用例"
            warnings.append(f"{path} 有改动 → 已选全量（这是唯一破例「严禁全量」的情形）")
            continue
        if path.startswith(IGNORED_PREFIXES) or path.endswith((".md", ".txt", ".bat", ".flag")):
            continue
        if path.startswith(("app/templates/", "app/static/")):
            for test in UI_TESTS:
                selected.setdefault(test, f"{path} 属于界面资产")
            continue
        if path.startswith("tests/"):
            if path.startswith("tests/test_"):
                selected[path] = "用例文件本身有改动"
            else:  # helper：选所有 import 它的测试
                importers = [t for t, imps in test_imports.items() if path in imps]
                if importers:
                    for test in importers:
                        selected[test] = f"import 了改动的 helper {path}"
                else:
                    warnings.append(f"{path} 没有测试 import 它，改动可能无覆盖")
            continue
        if path.startswith(("app/", "scripts/")) and path.endswith(".py"):
            relevant.add(path)
            for dependent in sorted(dependents.get(path, ())):
                relevant.add(dependent)
            if path.startswith("app/"):
                # 页面模块：按路由字面量反查 URL 驱动的测试
                literals = _route_literals(ROOT / path)
                for test, text in sources.items():
                    hit = sorted(lit for lit in literals if lit in text)
                    if hit:
                        selected.setdefault(
                            test, f"覆盖改动模块 {path} 声明的路由（如 {hit[0]}）")
            continue
        warnings.append(f"{path} 没有映射规则，拿不准请加 --all")

    if relevant:
        for test, imported in test_imports.items():
            hit = sorted(imported & relevant)
            if hit:
                selected.setdefault(test, "依赖改动模块：" + ", ".join(hit))

    return sorted(selected), selected, warnings


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按改动选跑相关测试（默认紧凑输出，省 token）")
    parser.add_argument("files", nargs="*", help="改动文件；不传则取 git status")
    parser.add_argument("--collect", action="store_true",
                        help="只列清单和原因，不执行 pytest")
    parser.add_argument("--all", action="store_true",
                        help="明确全量（约 8 分钟，慎用）")
    parser.add_argument("--full-tb", action="store_true",
                        help="完整 traceback（默认 --tb=line）")
    args = parser.parse_args(argv)

    if args.all:
        print("== 明确全量（用户指定 --all）==")
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=line", "-rf"],
            check=False, cwd=ROOT).returncode

    changed = args.files or _changed_files()
    if not changed:
        print("没有检测到改动文件（不在 git 仓库？请显式传文件参数）")
        return 0

    selected, reasons, warnings = select(changed)
    for warning in warnings:
        print(f"⚠ {warning}")
    if not selected:
        print("改动没有映射到任何测试文件；确认无需测试可直接提交，或 --all 全量")
        return 0

    print(f"== 改动 {len(changed)} 个文件 → 选跑 {len(selected)} 个测试文件 ==")
    for test in selected:
        print(f"  {test}  （{reasons[test]}）")
    if args.collect:
        return 0

    cmd = [sys.executable, "-m", "pytest", "-q", "-rf",
           "--tb=long" if args.full_tb else "--tb=line", *selected]
    print(f"\n$ {' '.join(cmd[2:])}\n")
    return subprocess.run(cmd, check=False, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
