import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type EdgeProps,
} from "@xyflow/react";
import type {
  RuntimeEdgePoint,
  RuntimeFlowEdge,
} from "../architecture/layout";

interface LabelPoint {
  x: number;
  y: number;
}

function distance(a: RuntimeEdgePoint, b: RuntimeEdgePoint): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

/** 选最长线段的中点放标签，减少多条边标签堆在同一位置的概率。 */
function labelPointFor(points: RuntimeEdgePoint[]): LabelPoint | null {
  if (points.length < 2) {
    return null;
  }
  let bestStart = points[0];
  let bestEnd = points[1];
  let bestLength = -1;
  for (let index = 1; index < points.length; index += 1) {
    const start = points[index - 1];
    const end = points[index];
    const length = distance(start, end);
    if (length > bestLength) {
      bestLength = length;
      bestStart = start;
      bestEnd = end;
    }
  }
  return {
    x: (bestStart.x + bestEnd.x) / 2,
    y: (bestStart.y + bestEnd.y) / 2,
  };
}

export function RuntimeOrthogonalEdge(props: EdgeProps<RuntimeFlowEdge>) {
  const {
    id,
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    markerEnd,
    style,
    data,
  } = props;
  const points = data?.points ?? [];
  const [fallbackPath, fallbackLabelX, fallbackLabelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 10,
  });
  const path =
    points.length >= 2
      ? points
          .map(
            (point, index) =>
              `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`,
          )
          .join(" ")
      : fallbackPath;
  const label = data?.label;
  const labelPoint = label
    ? (labelPointFor(points) ?? { x: fallbackLabelX, y: fallbackLabelY })
    : null;
  const labelVisible = Boolean(label && data?.labelVisible && labelPoint);

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {labelVisible && labelPoint && data ? (
        <EdgeLabelRenderer>
          <div
            className={`runtime-edge-label runtime-edge-label--${data.relation}${
              data.feedback ? " runtime-edge-label--feedback" : ""
            }${data.selected ? " is-selected" : ""}`}
            style={{
              transform: `translate(-50%, -50%) translate(${labelPoint.x}px, ${labelPoint.y}px)`,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
