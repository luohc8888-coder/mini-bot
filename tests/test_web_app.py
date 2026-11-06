"""Phase 4 FastAPI、Workspace 绑定与静态前端服务测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mini_harness.cli.main import parse_args
from mini_harness.web import create_app
from mini_harness.workspace import WorkspaceResolver


def _workspace(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceResolver(cwd=tmp_path).resolve(root), root


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _frontend(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>Test Explorer</h1>", encoding="utf-8")
    (frontend / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return frontend


def _project(tmp_path: Path):
    workspace, root = _workspace(tmp_path)
    _write(root, "pkg/__init__.py", '"""Package."""\n')
    _write(
        root,
        "pkg/service.py",
        'class Service:\n'
        '    """A service."""\n'
        '\n'
        '    def run(self) -> None:\n'
        '        pass\n'
        '\n'
        '\n'
        'def make_service() -> Service:\n'
        '    return Service()\n',
    )
    _write(
        root,
        "main.py",
        'from pkg.service import make_service\n'
        '\n'
        'def main() -> None:\n'
        '    make_service()\n',
    )
    return workspace, root


def test_web_app_binds_workspace_and_exposes_project(tmp_path: Path) -> None:
    workspace, root = _project(tmp_path)
    app = create_app(workspace, frontend_dir=_frontend(tmp_path))

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["workspace"] == str(root)

        project = client.get("/api/project")
        assert project.status_code == 200
        payload = project.json()
        assert payload["name"] == "project"
        assert payload["workspaceId"] == workspace.id
        assert payload["stats"]["modules"] >= 2
        assert payload["graphStats"]["nodes"] > 0


def test_web_app_graph_and_node_endpoints(tmp_path: Path) -> None:
    workspace, _ = _project(tmp_path)
    app = create_app(workspace, frontend_dir=_frontend(tmp_path))

    with TestClient(app) as client:
        graph = client.get("/api/graph")
        assert graph.status_code == 200
        data = graph.json()
        assert data["schemaVersion"] == 1
        assert data["nodes"]
        assert data["edges"]
        assert "unresolved" not in data
        assert client.get("/api/graph?include_unresolved=true").json()["unresolved"] == []

        node_id = next(
            node["id"] for node in data["nodes"] if node["kind"] == "class"
        )
        detail = client.get("/api/node", params={"node_id": node_id})
        assert detail.status_code == 200
        assert detail.json()["node"]["id"] == node_id
        assert detail.json()["children"]

        missing = client.get("/api/node", params={"node_id": "missing"})
        assert missing.status_code == 404


def test_web_app_serves_local_static_frontend(tmp_path: Path) -> None:
    workspace, _ = _project(tmp_path)
    app = create_app(workspace, frontend_dir=_frontend(tmp_path))

    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Test Explorer" in home.text
        asset = client.get("/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text


def test_web_cli_parses_command() -> None:
    args = parse_args(["web", "--workspace", "/tmp/project", "--port", "8877"])
    assert args.command == "web"
    assert args.host == "127.0.0.1"
    assert args.port == 8877
