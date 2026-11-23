"""Phase 5 单元测试：完整文件源码切换 (/api/source?full_file=true) 与只读 Git 视图 API。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from mini_harness.web.app import create_app


def _write(root: Path, rel_path: str, content: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def test_source_api_supports_full_file_toggle(tmp_path: Path) -> None:
    """/api/source?full_file=true 应返回含 import 的完整文件并保留符号区间。"""
    _write(
        tmp_path,
        "pkg/calc.py",
        (
            "import math\n"
            "\n"
            "DEFAULT_SCALE = 2\n"
            "\n"
            "def compute(x: int) -> int:\n"
            "    return int(math.fabs(x)) * DEFAULT_SCALE\n"
            "\n"
            "def trailing() -> int:\n"
            "    return 1\n"
        ),
    )

    app = create_app(tmp_path, use_sqlite_cache=False)
    client = TestClient(app)

    graph = client.get("/api/graph").json()
    compute_node = next(
        node for node in graph["nodes"] if node["name"] == "compute"
    )
    symbol_id = compute_node["id"]

    # 默认模式：仅返回符号区间
    sym_res = client.get("/api/source", params={"symbol_id": symbol_id})
    assert sym_res.status_code == 200
    sym_data = sym_res.json()
    assert sym_data["fullFile"] is False
    assert sym_data["startLine"] == sym_data["symbolStartLine"]
    assert sym_data["startLine"] > 1
    assert sym_data["endLine"] == sym_data["symbolEndLine"]

    # 完整文件模式：返回第 1 行到末行，同时保留 symbolStartLine / symbolEndLine
    full_res = client.get(
        "/api/source",
        params={"symbol_id": symbol_id, "full_file": "true"},
    )
    assert full_res.status_code == 200
    full_data = full_res.json()
    assert full_data["fullFile"] is True
    assert full_data["startLine"] == 1
    assert full_data["endLine"] == full_data["totalLines"]
    assert full_data["lines"][0]["text"] == "import math"
    assert full_data["symbolStartLine"] == sym_data["symbolStartLine"]
    assert full_data["symbolEndLine"] == sym_data["symbolEndLine"]


def test_git_api_gracefully_handles_non_git_workspace(tmp_path: Path) -> None:
    """非 Git 目录应优雅降级，/api/git/status 返回 available=False。"""
    _write(tmp_path, "app.py", "def main() -> None:\n    pass\n")
    app = create_app(tmp_path, use_sqlite_cache=False)
    client = TestClient(app)

    status_res = client.get("/api/git/status")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["available"] is False
    assert data["files"] == []
    assert data["summary"]["total"] == 0

    diff_res = client.get("/api/git/diff", params={"file": "app.py"})
    assert diff_res.status_code == 503


def test_git_status_diff_and_security_boundaries(tmp_path: Path) -> None:
    """真实 Git 仓库应正确返回分支、变更状态、结构化 Diff、关联符号并拦截非法路径。"""
    _write(
        tmp_path,
        "pkg/service.py",
        (
            "def greet(name: str) -> str:\n"
            "    return f'hello {name}'\n"
        ),
    )
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test Bot")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial commit")

    # 修改已追踪文件 + 新增未追踪文件
    _write(
        tmp_path,
        "pkg/service.py",
        (
            "def greet(name: str) -> str:\n"
            "    return f'hi {name}!'\n\n"
            "def farewell(name: str) -> str:\n"
            "    return f'bye {name}'\n"
        ),
    )
    _write(
        tmp_path,
        "pkg/new_helper.py",
        (
            "def helper() -> int:\n"
            "    return 42\n"
        ),
    )

    app = create_app(tmp_path, use_sqlite_cache=False)
    client = TestClient(app)

    status_res = client.get("/api/git/status")
    assert status_res.status_code == 200
    status = status_res.json()
    assert status["available"] is True
    assert status["branch"]
    assert status["head"]["subject"] == "initial commit"
    assert status["summary"]["modified"] == 1
    assert status["summary"]["untracked"] == 1
    assert status["summary"]["total"] == 2

    by_path = {item["path"]: item for item in status["files"]}
    assert by_path["pkg/service.py"]["status"] == "M"
    assert by_path["pkg/service.py"]["symbolId"] is not None
    assert by_path["pkg/new_helper.py"]["status"] == "?"
    assert by_path["pkg/new_helper.py"]["symbolId"] is not None

    # 已修改文件 Diff
    mod_diff_res = client.get("/api/git/diff", params={"file": "pkg/service.py"})
    assert mod_diff_res.status_code == 200
    mod_diff = mod_diff_res.json()
    assert mod_diff["additions"] >= 3
    assert mod_diff["deletions"] >= 1
    assert any(line["kind"] == "add" and "farewell" in line["text"] for line in mod_diff["lines"])
    assert any(line["kind"] == "del" and "hello" in line["text"] for line in mod_diff["lines"])
    assert any(sym["name"] == "farewell" for sym in mod_diff["symbols"])

    # 未追踪新文件 Diff 预览
    new_diff_res = client.get("/api/git/diff", params={"file": "pkg/new_helper.py"})
    assert new_diff_res.status_code == 200
    new_diff = new_diff_res.json()
    assert new_diff["category"] == "untracked"
    assert new_diff["additions"] == 2
    assert any(line["kind"] == "add" and "return 42" in line["text"] for line in new_diff["lines"])

    # 安全边界：禁止路径穿越与 git 参数注入
    traversal = client.get("/api/git/diff", params={"file": "../outside.py"})
    assert traversal.status_code == 400
    flag_inject = client.get("/api/git/diff", params={"file": "--exec=whoami"})
    assert flag_inject.status_code == 400
