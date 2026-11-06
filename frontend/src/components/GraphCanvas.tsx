import { useEffect } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type NodeTypes,
} from "@xyflow/react";
import { ArchitectureNode } from "./ArchitectureNode";
import {
  layoutGraph,
  type ArchitectureFlowNode,
  type ArchitectureNodeData,
} from "../graph/layout";
import type { MockEdge, MockNode, NodeKind } from "../types/graph";

const nodeTypes: NodeTypes = {
  architecture: ArchitectureNode,
};

const MINIMAP_COLORS: Record<NodeKind, string> = {
  module: "#5b8cff",
  class: "#b388ff",
  function: "#37d6a0",
  method: "#f2c94c",
};

interface GraphCanvasProps {
  viewKey: string;
  nodes: MockNode[];
  edges: MockEdge[];
  selectedId: string | null;
  onNodeClick: (nodeId: string) => void;
  onPaneClick: () => void;
}

export function GraphCanvas({
  viewKey,
  nodes,
  edges,
  selectedId,
  onNodeClick,
  onPaneClick,
}: GraphCanvasProps) {
  const [flowNodes, setFlowNodes, onNodesChange] =
    useNodesState<ArchitectureFlowNode>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView } = useReactFlow();

  // 只在层级变化时重新布局；拖拽位置在 React Flow 内部保持。
  useEffect(() => {
    const layout = layoutGraph(nodes, edges);
    setFlowNodes(layout.nodes);
    setFlowEdges(layout.edges);

    const timer = window.setTimeout(() => {
      void fitView({ padding: 0.22, duration: 320 });
    }, 40);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey, setFlowNodes, setFlowEdges, fitView]);

  useEffect(() => {
    setFlowNodes((current) =>
      current.map((node) => ({
        ...node,
        selected: node.id === selectedId,
      })),
    );
  }, [selectedId, setFlowNodes]);

  return (
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      onNodeClick={(_, node) => onNodeClick(node.id)}
      onPaneClick={onPaneClick}
      fitView
      fitViewOptions={{ padding: 0.22 }}
      minZoom={0.28}
      maxZoom={1.8}
      nodesConnectable={false}
      elementsSelectable
      selectionOnDrag={false}
      deleteKeyCode={null}
      proOptions={{ hideAttribution: false }}
    >
      <Background
        variant={BackgroundVariant.Dots}
        gap={24}
        size={1.2}
        color="#263653"
      />
      <Controls position="top-right" showInteractive={false} />
      <MiniMap
        pannable
        zoomable
        nodeColor={(node) =>
          MINIMAP_COLORS[
            (node.data as ArchitectureNodeData).kind as NodeKind
          ] ?? "#5b8cff"
        }
        maskColor="rgba(4, 8, 16, 0.72)"
        style={{
          background: "#0b1424",
          border: "1px solid #24334c",
          borderRadius: 10,
        }}
      />
    </ReactFlow>
  );
}
