import {
  buildDagLayoutFromView,
  P6_NODE_SIZE,
  type P6TaskView,
} from "./model";
import type { RunView } from "../data/core/run-view";

interface P6DagProps {
  view: RunView;
  tasks: P6TaskView[];
  selectedTaskId: string;
  onSelect: (taskId: string) => void;
}

function escapeSvg(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function truncate(value: string, maxLength: number): string {
  return value.length <= maxLength
    ? value
    : `${value.slice(0, maxLength - 1)}…`;
}

export function P6Dag({
  view,
  tasks,
  selectedTaskId,
  onSelect,
}: P6DagProps) {
  const layout = buildDagLayoutFromView(view);
  const edges = tasks.flatMap(({ task }) =>
    task.depends_on.map((dependency) => ({
      from: dependency,
      to: task.task_id,
    })),
  );

  return (
    <div className="p6-dag-scroll">
      <svg
        className="p6-dag"
        style={{ minWidth: Math.min(Math.round(layout.width * 0.72), 930) }}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label="只读业务任务依赖图"
      >
        <defs>
          <marker
            id="p6-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          <marker
            id="p6-arrow-critical"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <g>
          {edges.map((edge) => {
            const from = layout.positions.get(edge.from);
            const to = layout.positions.get(edge.to);
            if (!from || !to) return null;
            const startX = from.x + P6_NODE_SIZE.width;
            const startY = from.y + P6_NODE_SIZE.height / 2;
            const endX = to.x;
            const endY = to.y + P6_NODE_SIZE.height / 2;
            const offset = Math.max(24, (endX - startX) / 2);
            const critical = layout.criticalPairs.has(
              `${edge.from}->${edge.to}`,
            );
            return (
              <path
                key={`${edge.from}-${edge.to}`}
                className={`p6-edge${critical ? " p6-edge-critical" : ""}`}
                d={`M ${startX} ${startY} C ${startX + offset} ${startY}, ${endX - offset} ${endY}, ${endX} ${endY}`}
                markerEnd={`url(#${critical ? "p6-arrow-critical" : "p6-arrow"})`}
              />
            );
          })}
        </g>
        <g>
          {tasks.map(({ task, runtime }) => {
            const position = layout.positions.get(task.task_id);
            if (!position) return null;
            const selected = task.task_id === selectedTaskId;
            return (
              <g
                key={task.task_id}
                className={[
                  "p6-dag-node",
                  `p6-state-${runtime.state}`,
                  selected ? "p6-selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                transform={`translate(${position.x} ${position.y})`}
                data-p6-task={task.task_id}
                role="button"
                tabIndex={0}
                aria-label={`${task.task_id} ${task.goal} ${runtime.state}`}
                onClick={() => onSelect(task.task_id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(task.task_id);
                  }
                }}
              >
                <rect
                  width={P6_NODE_SIZE.width}
                  height={P6_NODE_SIZE.height}
                  rx="7"
                />
                <text className="p6-node-id" x="13" y="19">
                  {escapeSvg(task.task_id)}
                </text>
                <circle className="p6-node-dot" cx="156" cy="15" r="4" />
                <text className="p6-node-title" x="13" y="38">
                  {escapeSvg(truncate(task.goal, 16))}
                </text>
                <text className="p6-node-meta" x="13" y="53">
                  {escapeSvg(task.executor)} · {escapeSvg(runtime.state)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
