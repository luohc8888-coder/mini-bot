import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { ArchitectureFlowNode } from "../graph/layout";
import type { NodeKind } from "../types/graph";

const KIND_ICONS: Record<NodeKind, string> = {
  module: "▣",
  class: "◆",
  function: "ƒ",
  method: "↳",
};

export function ArchitectureNode({
  data,
  selected,
}: NodeProps<ArchitectureFlowNode>) {
  return (
    <div
      className={`arch-node arch-node--${data.kind}${selected ? " is-selected" : ""}`}
      title={data.summary}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="arch-node__handle"
        isConnectable={false}
      />
      <div className="arch-node__top">
        <span className="arch-node__icon" aria-hidden="true">
          {KIND_ICONS[data.kind]}
        </span>
        <span className="arch-node__kind">{data.kindLabel}</span>
      </div>
      <div className="arch-node__title">{data.label}</div>
      <div className="arch-node__meta">{data.meta}</div>
      <div className="arch-node__footer">
        <span className="arch-node__summary">{data.childSummary}</span>
        <span className="arch-node__action">
          {data.hasChildren ? "下钻 →" : "查看源码"}
        </span>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="arch-node__handle"
        isConnectable={false}
      />
    </div>
  );
}
