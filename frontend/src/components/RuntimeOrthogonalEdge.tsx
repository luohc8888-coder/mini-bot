import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  Position,
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

const HANDLE_TOLERANCE = 3;

function samePoint(a: RuntimeEdgePoint, b: RuntimeEdgePoint): boolean {
  return (
    Math.abs(a.x - b.x) < HANDLE_TOLERANCE &&
    Math.abs(a.y - b.y) < HANDLE_TOLERANCE
  );
}

function dedupePoints(points: RuntimeEdgePoint[]): RuntimeEdgePoint[] {
  const deduped = points.filter(
    (point, index) => index === 0 || !samePoint(point, points[index - 1]),
  );
  // 移除拖拽后产生的同向共线冗余节点。
  return deduped.filter((point, index) => {
    if (index === 0 || index === deduped.length - 1) {
      return true;
    }
    const previous = deduped[index - 1];
    const next = deduped[index + 1];
    return !(
      (Math.abs(previous.x - point.x) < 0.5 &&
        Math.abs(point.x - next.x) < 0.5) ||
      (Math.abs(previous.y - point.y) < 0.5 &&
        Math.abs(point.y - next.y) < 0.5)
    );
  });
}

function orthogonalConnector(
  from: RuntimeEdgePoint,
  to: RuntimeEdgePoint,
  side: Position | undefined,
  forTarget: boolean,
): RuntimeEdgePoint[] {
  if (samePoint(from, to)) {
    return [to];
  }
  const horizontal = side === Position.Left || side === Position.Right;
  if (horizontal) {
    if (forTarget) {
      // 最终一段水平进入 target。
      return [{ x: from.x, y: to.y }, to];
    }
    // 从 source 先沿水平方向离开。
    return [{ x: to.x, y: from.y }, to];
  }
  if (forTarget) {
    // 最终一段垂直进入 target。
    return [{ x: to.x, y: from.y }, to];
  }
  return [{ x: from.x, y: to.y }, to];
}

/**
 * ELK 的 points 是“初始布局”的路径。拖拽节点后 React Flow 的
 * sourceX/sourceY/targetX/targetY 会变化，但 points 不会自动变。
 * 这里保留中间 bend points，并用正交连接线重新接到当前端点，
 * 这样拖拽过程中边始终跟随节点，同时不丢掉 ELK 的折线路径。
 */
function adaptPointsToHandles(
  points: RuntimeEdgePoint[],
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  sourcePosition: Position | undefined,
  targetPosition: Position | undefined,
): RuntimeEdgePoint[] {
  if (points.length < 2) {
    return [];
  }
  const oldSource = points[0];
  const oldTarget = points[points.length - 1];
  const sourceMoved =
    Math.abs(oldSource.x - sourceX) > HANDLE_TOLERANCE ||
    Math.abs(oldSource.y - sourceY) > HANDLE_TOLERANCE;
  const targetMoved =
    Math.abs(oldTarget.x - targetX) > HANDLE_TOLERANCE ||
    Math.abs(oldTarget.y - targetY) > HANDLE_TOLERANCE;
  if (!sourceMoved && !targetMoved) {
    return points;
  }

  let result = points.map((point) => ({ ...point }));
  if (sourceMoved) {
    const anchor = result[1] ?? { x: targetX, y: targetY };
    const connector = orthogonalConnector(
      { x: sourceX, y: sourceY },
      anchor,
      sourcePosition,
      false,
    );
    result = [
      { x: sourceX, y: sourceY },
      ...connector,
      ...result.slice(1),
    ];
  }
  if (targetMoved) {
    const anchor = result[result.length - 2] ?? { x: sourceX, y: sourceY };
    const connector = orthogonalConnector(
      anchor,
      { x: targetX, y: targetY },
      targetPosition,
      true,
    );
    result = [
      ...result.slice(0, -1),
      ...connector,
      { x: targetX, y: targetY },
    ];
  }
  return dedupePoints(result);
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
  const points = adaptPointsToHandles(
    data?.points ?? [],
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  );
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
