"""Phase 2：Tree-sitter TypeScript/TSX 适配器与全栈 CodeGraph（含跨端 API 桥接）测试。"""

from __future__ import annotations

from pathlib import Path

from mini_harness.explorer import (
    SQLiteIndexStore,
    build_code_graph,
    scan_project,
)
from mini_harness.workspace import WorkspaceResolver


def _make_fullstack_workspace(tmp_path: Path):
    root = tmp_path / "fullstack"
    root.mkdir()

    # 后端 FastAPI 路由
    (root / "backend").mkdir()
    (root / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (root / "backend" / "app.py").write_text(
        "class FakeApp:\n"
        "    def post(self, route: str):\n"
        "        return lambda fn: fn\n"
        "    def get(self, route: str):\n"
        "        return lambda fn: fn\n\n"
        "app = FakeApp()\n\n"
        '@app.get("/api/items")\n'
        "def list_items() -> list[str]:\n"
        '    """返回项目列表。"""\n'
        "    return []\n\n"
        '@app.post("/api/chat")\n'
        "def chat_reply() -> dict[str, str]:\n"
        '    """处理源码问答请求。"""\n'
        '    return {"ok": "1"}\n',
        encoding="utf-8",
    )

    # 前端 TS/TSX
    (root / "frontend" / "src" / "components").mkdir(parents=True)
    (root / "frontend" / "src" / "types.ts").write_text(
        "/** 列表项类型定义 */\n"
        "export interface ItemRecord {\n"
        "  id: string;\n"
        "  title: string;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "frontend" / "src" / "api.ts").write_text(
        'import type { ItemRecord } from "./types";\n\n'
        "/** API 客户端 */\n"
        "export class ApiClient {\n"
        "  request(path: string): Promise<Response> {\n"
        "    return fetch(path);\n"
        "  }\n\n"
        "  async loadItems(): Promise<ItemRecord[]> {\n"
        '    const res = await this.request("/api/items");\n'
        "    return res.json();\n"
        "  }\n"
        "}\n\n"
        "export const sendChat = async (q: string) => {\n"
        '  return fetch(`/api/chat?q=${q}`, { method: "POST" });\n'
        "};\n",
        encoding="utf-8",
    )
    (root / "frontend" / "src" / "components" / "ChatBox.tsx").write_text(
        'import { sendChat } from "../api";\n'
        'import "./chat.css";\n\n'
        "/** 源码聊天组件 */\n"
        "export function ChatBox() {\n"
        '  const onSend = () => sendChat("hello");\n'
        "  return <button onClick={onSend}>Send</button>;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "frontend" / "src" / "App.tsx").write_text(
        'import { ChatBox } from "./components/ChatBox";\n'
        'import { ApiClient } from "./api";\n\n'
        "export default function App() {\n"
        "  const client = new ApiClient();\n"
        "  void client;\n"
        "  return <ChatBox />;\n"
        "}\n",
        encoding="utf-8",
    )

    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)
    return workspace, root


def _node_by_qname(graph, qname: str):
    for node in graph.nodes.values():
        if node.qualified_name == qname:
            return node
    raise AssertionError(f"未找到符号：{qname}")


def test_typescript_parser_extracts_symbols_imports_jsx_and_api_bridges(
    tmp_path: Path,
) -> None:
    workspace, _ = _make_fullstack_workspace(tmp_path)
    index = scan_project(workspace)
    graph = build_code_graph(index)

    languages = {f.language for f in index.files}
    assert languages == {"python", "typescript"}
    assert not index.diagnostics
    assert graph.stats["unresolvedImports"] == 0

    # 1. Interface / Class / Method / Arrow Function / React Component 全部入图
    iface = _node_by_qname(graph, "frontend.src.types.ItemRecord")
    assert iface.kind == "class"
    assert iface.language == "typescript"
    assert "列表项类型定义" in (iface.docstring or "")

    client_cls = _node_by_qname(graph, "frontend.src.api.ApiClient")
    load_items = _node_by_qname(graph, "frontend.src.api.ApiClient.loadItems")
    request_method = _node_by_qname(graph, "frontend.src.api.ApiClient.request")
    send_chat = _node_by_qname(graph, "frontend.src.api.sendChat")
    chat_box = _node_by_qname(graph, "frontend.src.components.ChatBox.ChatBox")
    app_fn = _node_by_qname(graph, "frontend.src.App.App")

    assert load_items.parent_id == client_cls.id
    # this.request(...) 静态解析
    assert request_method.id in {n.id for n in graph.get_callees(load_items.id)}
    # React JSX <ChatBox /> 解析为 calls 边
    assert chat_box.id in {n.id for n in graph.get_callees(app_fn.id)}
    # 跨文件箭头函数调用 sendChat
    assert send_chat.id in {n.id for n in graph.get_callees(chat_box.id)}

    # 2. 跨端 HTTP API 静态桥接：TS -> Python FastAPI handler
    py_list_items = _node_by_qname(graph, "backend.app.list_items")
    py_chat_reply = _node_by_qname(graph, "backend.app.chat_reply")
    assert py_list_items.id in {n.id for n in graph.get_callees(load_items.id)}
    assert py_chat_reply.id in {n.id for n in graph.get_callees(send_chat.id)}
    assert send_chat.id in {n.id for n in graph.get_callers(py_chat_reply.id)}


def test_typescript_syntax_error_recorded_without_crashing_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "broken_ts"
    root.mkdir()
    (root / "good.ts").write_text("export function ok(): number { return 1; }\n", encoding="utf-8")
    (root / "bad.ts").write_text("export function broken( { return ;;;\n", encoding="utf-8")
    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)

    index = scan_project(workspace)
    by_path = {f.path: f for f in index.files}
    assert by_path["good.ts"].parse_status == "ok"
    assert by_path["bad.ts"].parse_status == "error"
    assert any(d.code == "syntax_error" and d.file_path == "bad.ts" for d in index.diagnostics)
    assert any(s.name == "ok" for s in index.symbols)


def test_sqlite_hybrid_search_indexes_typescript_and_cross_stack_neighbors(
    tmp_path: Path,
) -> None:
    workspace, _ = _make_fullstack_workspace(tmp_path)
    store = SQLiteIndexStore(workspace, base_dir=tmp_path / "db")
    result = store.sync()

    assert result.stats.total_files == 6
    ts_hits = store.search("源码聊天 ChatBox", limit=5)
    assert ts_hits
    assert ts_hits[0].file_path == "frontend/src/components/ChatBox.tsx"

    # 搜索后端 chat_reply，可直接看到前端 sendChat 作为 caller
    backend_hits = store.search("chat_reply 源码问答", limit=5)
    assert backend_hits
    top_backend = next(h for h in backend_hits if h.name == "chat_reply")
    assert any("sendChat" in c for c in top_backend.callers)
