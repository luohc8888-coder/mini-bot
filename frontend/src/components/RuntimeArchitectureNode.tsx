import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { RuntimeFlowNode } from "../architecture/layout";

const KIND_ICONS = {
  component: "◆",
  stage: "▸",
  condition: "◇",
} as const;

export function RuntimeArchitectureNode({
  data,
  selected,
}: NodeProps<RuntimeFlowNode>) {
  const sources = (data.sourceRefs ?? []).filter((item) => item.symbolId);
  const source = sources[0];
  return (
    <div
      className={`runtime-node runtime-node--${data.kind}${
        selected ? " is-selected" : ""
      }`}
      title={data.description}
    >
      <Handle
        id="target-left"
        type="target"
        position={Position.Left}
        className="runtime-node__handle"
        isConnectable={false}
      />
      <Handle
        id="target-right"
        type="target"
        position={Position.Right}
        className="runtime-node__handle"
        isConnectable={false}
      />
      <Handle
        id="source-left"
        type="source"
        position={Position.Left}
        className="runtime-node__handle"
        isConnectable={false}
      />
      <div className="runtime-node__top">
        <span className="runtime-node__icon" aria-hidden="true">
          {KIND_ICONS[data.kind]}
        </span>
        <span className="runtime-node__kind">{data.kindLabel}</span>
        {data.mock ? <span className="runtime-node__mock">MOCK</span> : null}
      </div>

      <div className="runtime-node__title">{data.label}</div>
      <div className="runtime-node__description">{data.description}</div>

      {data.nested.length > 0 ? (
        <div className="runtime-node__nested">
          {data.nested.map((nested) => (
            <button
              key={nested.id}
              type="button"
              className="runtime-node__nested-card"
              disabled={!nested.detailView}
              onClick={(event) => {
                event.stopPropagation();
                if (nested.detailView) {
                  data.onNestedClick?.(nested.detailView);
                }
              }}
            >
              <span className="runtime-node__nested-mark" aria-hidden="true">
                {KIND_ICONS[nested.kind]}
              </span>
              <span className="runtime-node__nested-copy">
                <strong>{nested.label}</strong>
                <span>嵌套组件 · 点击查看内部流程</span>
              </span>
              <span className="runtime-node__nested-action" aria-hidden="true">
                →
              </span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="runtime-node__footer">
        <span className="runtime-node__meta">{data.meta || "逻辑节点"}</span>
        <span className="runtime-node__footer-actions">
          {source ? (
            <button
              type="button"
              className="runtime-node__source-link"
              onClick={(event) => {
                event.stopPropagation();
                data.onSourceOpen?.(source);
              }}
              title="打开该节点的核心源码"
            >
              {sources.length > 1
                ? `查看核心源码 · ${sources.length}`
                : "查看核心源码"}
            </button>
          ) : null}
          <span className="runtime-node__action">
            {data.detailView ? "双击进入内部流程 →" : "查看职责"}
          </span>
        </span>
      </div>

      <Handle
        id="source-right"
        type="source"
        position={Position.Right}
        className="runtime-node__handle"
        isConnectable={false}
      />
    </div>
  );
}
