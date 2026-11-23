"""Phase 2 扩展：Java + JavaScript/JSX（含 Spring Boot 跨端 API 桥接）测试。"""

from __future__ import annotations

from pathlib import Path

from mini_harness.explorer import (
    SQLiteIndexStore,
    build_code_graph,
    scan_project,
)
from mini_harness.workspace import WorkspaceResolver


def _make_java_js_workspace(tmp_path: Path):
    root = tmp_path / "spring_shop"
    root.mkdir()

    java_root = root / "src" / "main" / "java" / "com" / "example"
    (java_root / "common").mkdir(parents=True)
    (java_root / "order").mkdir(parents=True)
    (root / "web").mkdir(parents=True)

    # Java common package (for wildcard import)
    (java_root / "common" / "IdGenerator.java").write_text(
        "package com.example.common;\n\n"
        "/** 全局 ID 生成器 */\n"
        "public class IdGenerator {\n"
        "    public static String nextId() {\n"
        '        return "ORD-1";\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    # Java same-package helper (no explicit import needed)
    (java_root / "order" / "OrderValidator.java").write_text(
        "package com.example.order;\n\n"
        "public class OrderValidator {\n"
        "    public static boolean check(int limit) {\n"
        "        return limit > 0;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    # Java Service
    (java_root / "order" / "OrderService.java").write_text(
        "package com.example.order;\n\n"
        "import com.example.common.*;\n\n"
        "/** 订单核心业务服务 */\n"
        "public class OrderService {\n"
        "    public String listOrders(int limit) {\n"
        "        if (!OrderValidator.check(limit)) {\n"
        '            return "";\n'
        "        }\n"
        "        return IdGenerator.nextId();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    # Java Spring Controller
    (java_root / "order" / "OrderController.java").write_text(
        "package com.example.order;\n\n"
        "/** 订单 HTTP 控制器 */\n"
        "@RestController\n"
        '@RequestMapping("/api/orders")\n'
        "public class OrderController {\n"
        "    private final OrderService orderService;\n\n"
        "    public OrderController(OrderService orderService) {\n"
        "        this.orderService = orderService;\n"
        "    }\n\n"
        '    @GetMapping("/list")\n'
        "    public String fetchOrders(int limit) {\n"
        "        return this.orderService.listOrders(limit);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    # JavaScript + JSX + CommonJS require
    (root / "web" / "format.js").write_text(
        "/** 格式化价格 */\n"
        "export function formatPrice(amount) {\n"
        "  return `$${amount}`;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "web" / "orders.jsx").write_text(
        'import { formatPrice } from "./format";\n\n'
        "/** 订单列表前端组件 */\n"
        "export function OrderPanel() {\n"
        '  fetch("/api/orders/list?limit=10");\n'
        "  return <div>{formatPrice(99)}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "web" / "cjs_runner.cjs").write_text(
        'const { formatPrice } = require("./format");\n\n'
        "function runSummary() {\n"
        "  return formatPrice(42);\n"
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


def test_java_and_javascript_full_stack_codegraph(tmp_path: Path) -> None:
    workspace, _ = _make_java_js_workspace(tmp_path)
    index = scan_project(workspace)
    graph = build_code_graph(index)

    assert {f.language for f in index.files} == {"java", "javascript"}
    assert not index.diagnostics
    assert graph.stats["unresolvedImports"] == 0

    ctrl_fetch = _node_by_qname(
        graph, "com.example.order.OrderController.OrderController.fetchOrders"
    )
    svc_list = _node_by_qname(
        graph, "com.example.order.OrderService.OrderService.listOrders"
    )
    validator_check = _node_by_qname(
        graph, "com.example.order.OrderValidator.OrderValidator.check"
    )
    id_next = _node_by_qname(
        graph, "com.example.common.IdGenerator.IdGenerator.nextId"
    )
    order_panel = _node_by_qname(graph, "web.orders.OrderPanel")
    format_price = _node_by_qname(graph, "web.format.formatPrice")
    cjs_run = _node_by_qname(graph, "web.cjs_runner.runSummary")

    # 1. Java 字段类型推导：this.orderService.listOrders() -> OrderService.listOrders
    assert svc_list.id in {n.id for n in graph.get_callees(ctrl_fetch.id)}
    # 2. Java 同包隐式可见：OrderValidator.check()
    assert validator_check.id in {n.id for n in graph.get_callees(svc_list.id)}
    # 3. Java 通配包导入：import com.example.common.*; -> IdGenerator.nextId()
    assert id_next.id in {n.id for n in graph.get_callees(svc_list.id)}
    # 4. JS ESM + CommonJS require 调用解析
    assert format_price.id in {n.id for n in graph.get_callees(order_panel.id)}
    assert format_price.id in {n.id for n in graph.get_callees(cjs_run.id)}
    # 5. 跨端 Spring Boot HTTP 路由桥接：JSX fetch("/api/orders/list") -> Java Controller
    assert ctrl_fetch.id in {n.id for n in graph.get_callees(order_panel.id)}
    assert order_panel.id in {n.id for n in graph.get_callers(ctrl_fetch.id)}


def test_java_syntax_error_isolated_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "broken_java"
    root.mkdir()
    (root / "Good.java").write_text(
        "public class Good { public void ok() {} }\n", encoding="utf-8"
    )
    (root / "Broken.java").write_text(
        "public class Broken { public void bad( { }\n", encoding="utf-8"
    )
    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)
    index = scan_project(workspace)
    by_path = {f.path: f for f in index.files}
    assert by_path["Good.java"].parse_status == "ok"
    assert by_path["Broken.java"].parse_status == "error"
    assert any(
        d.code == "syntax_error" and d.file_path == "Broken.java"
        for d in index.diagnostics
    )


def test_sqlite_search_spans_java_and_javascript_call_chain(
    tmp_path: Path,
) -> None:
    workspace, _ = _make_java_js_workspace(tmp_path)
    store = SQLiteIndexStore(workspace, base_dir=tmp_path / "db")
    result = store.sync()
    assert result.stats.total_files == 7

    hits = store.search("订单 控制器 fetchOrders", limit=5)
    assert hits
    ctrl_hit = next(h for h in hits if h.name == "fetchOrders")
    assert ctrl_hit.language == "java"
    assert any("OrderPanel" in c for c in ctrl_hit.callers)
    assert any("listOrders" in c for c in ctrl_hit.callees)
