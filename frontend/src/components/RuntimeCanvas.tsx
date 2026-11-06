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
import { RuntimeArchitectureNode } from "./RuntimeArchitectureNode";
import {
  buildRuntimeFlowEdge,
  buildRuntimeFlowNodes,
  type RuntimeFlowNode,
  type RuntimeNodeData,
} from "../architecture/layout";
import type { ArchitectureView } from "../types/architecture";

const nodeTypes: NodeTypes = {
  runtime: RuntimeArchitectureNode,
};

const MINIMAP_COLORS: Record<string, string> = {
  component: "#b88cff",
  stage: "#6f9cff",
  condition: "#f2c94c",
};

const FIT_VIEW_OPTIONS = {
  padding: 0.08,
};

interface RuntimeCanvasProps {
  view: ArchitectureView;
  graphMock: boolean;
  selectedId: string | null;
  onNodeClick: (nodeId: string) => void;
  onNestedClick: (detailView: string) => void;
  onPaneClick: () => void;
}

export function RuntimeCanvas({
  view,
  graphMock,
  selectedId,
  onNodeClick,
  onNestedClick,
  onPaneClick,
}: RuntimeCanvasProps) {
  const [flowNodes, setFlowNodes, onNodesChange] =
    useNodesState<RuntimeFlowNode>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView } = useReactFlow();

  useEffect(() => {
    setFlowNodes(buildRuntimeFlowNodes(view, graphMock, onNestedClick));
    setFlowEdges(view.edges.map(buildRuntimeFlowEdge));

    const timer = window.setTimeout(() => {
      void fitView({ ...FIT_VIEW_OPTIONS, duration: 320 });
    }, 40);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, graphMock, onNestedClick, setFlowNodes, setFlowEdges, fitView]);

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
      onNodeClick={(_, node) => onNodeClick(node.id)}
      onPaneClick={onPaneClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={FIT_VIEW_OPTIONS}
      minZoom={0.2}
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
          MINIMAP_COLORS[(node.data as RuntimeNodeData).kind] ?? "#6f9cff"
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
