import { useEffect, useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type NodeTypes,
} from "@xyflow/react";
import { RuntimeArchitectureNode } from "./RuntimeArchitectureNode";
import { RuntimeOrthogonalEdge } from "./RuntimeOrthogonalEdge";
import { layoutRuntimeView } from "../architecture/elkLayout";
import {
  buildRuntimeFlowEdge,
  detectFeedbackEdgeIds,
  isEdgeVisible,
  type RelationMode,
  type RuntimeEdgeRoute,
  type RuntimeFlowEdge,
  type RuntimeFlowNode,
  type RuntimeNodeData,
} from "../architecture/layout";
import type {
  ArchitectureSourceRef,
  ArchitectureView,
} from "../types/architecture";

const nodeTypes: NodeTypes = {
  runtime: RuntimeArchitectureNode,
};

const edgeTypes = {
  orthogonal: RuntimeOrthogonalEdge,
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
  relationMode: RelationMode;
  onNodeClick: (nodeId: string) => void;
  onNodeDoubleClick: (nodeId: string) => void;
  onNestedClick: (detailView: string) => void;
  onSourceOpen: (source: ArchitectureSourceRef) => void;
  onPaneClick: () => void;
}

export function RuntimeCanvas({
  view,
  graphMock,
  selectedId,
  relationMode,
  onNodeClick,
  onNodeDoubleClick,
  onNestedClick,
  onSourceOpen,
  onPaneClick,
}: RuntimeCanvasProps) {
  const [flowNodes, setFlowNodes, onNodesChange] =
    useNodesState<RuntimeFlowNode>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] =
    useEdgesState<RuntimeFlowEdge>([]);
  const [edgeRoutes, setEdgeRoutes] = useState<Record<string, RuntimeEdgeRoute>>(
    {},
  );
  const { fitView } = useReactFlow();
  const feedbackEdgeIds = useMemo(
    () => detectFeedbackEdgeIds(view.edges),
    [view.edges],
  );

  useEffect(() => {
    let cancelled = false;
    let fitTimer: number | undefined;
    setEdgeRoutes({});

    void layoutRuntimeView(view, {
      mock: graphMock,
      onNestedClick,
      onSourceOpen,
    }).then((result) => {
      if (cancelled) {
        return;
      }
      setFlowNodes(result.nodes);
      setEdgeRoutes(result.edgeRoutes);
      fitTimer = window.setTimeout(() => {
        void fitView({ ...FIT_VIEW_OPTIONS, duration: 320 });
      }, 40);
    });

    return () => {
      cancelled = true;
      if (fitTimer !== undefined) {
        window.clearTimeout(fitTimer);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, graphMock, onNestedClick, onSourceOpen, setFlowNodes, fitView]);

  useEffect(() => {
    setFlowEdges(
      view.edges
        .filter((edge) => isEdgeVisible(edge, relationMode))
        .map((edge) =>
          buildRuntimeFlowEdge(edge, {
            relationMode,
            selectedId,
            route: edgeRoutes[edge.id],
            feedback: feedbackEdgeIds.has(edge.id),
          }),
        ),
    );
  }, [
    view,
    relationMode,
    selectedId,
    edgeRoutes,
    feedbackEdgeIds,
    setFlowEdges,
  ]);

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
      onNodeDoubleClick={(_, node) => onNodeDoubleClick(node.id)}
      onPaneClick={onPaneClick}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
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
