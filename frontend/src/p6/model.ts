import scenariosFixture from "../../../fixtures/contracts/scenarios.json";
import compiledManifestFixture from "../../../fixtures/plans/compiled_manifest.json";
import planFixture from "../../../fixtures/plans/standard_plan.json";
import traceabilityFixture from "../../../fixtures/plans/traceability_matrix.json";
import type {
  Command,
  Plan,
  Run,
  Task,
  TaskRuntime,
} from "../contracts/generated";
import { parseSnapshot } from "../data/core/events";
import type {
  AllowedAction,
  ProjectionTask,
  RunView,
} from "../data/core/run-view";

export type P6ScenarioKey =
  | "normal"
  | "blocked"
  | "awaiting_review"
  | "risk_accepted"
  | "stale"
  | "outcome_unknown"
  | "completed";

type ScenarioFixture = Record<P6ScenarioKey, Run>;

export interface P6Scenario {
  key: P6ScenarioKey;
  label: string;
  description: string;
  run: Run;
}

const scenarioFixtures = scenariosFixture as unknown as ScenarioFixture;
const planBundle = planFixture as unknown as {
  plan: Plan;
  edge_count: number;
};
const manifest = compiledManifestFixture as unknown as {
  layers: string[][];
  topological_order: string[];
  edge_count: number;
  critical_path: {
    heuristic_kind: string;
    critical_chain: string[];
    disclaimer: string;
  };
};
const traceability = traceabilityFixture as unknown as {
  rows: P6TraceFixtureRow[];
  coverage_complete: boolean;
  uncovered_requirements: string[];
  out_of_scope_hits: string[];
};

export interface P6TraceFixtureRow {
  req_id: string;
  req_text: string;
  ds_ids: string[];
  all_tasks: string[];
  stage_status: string;
  verification_status: string;
  accepted: boolean;
}

const scenarioMeta: Array<Omit<P6Scenario, "run">> = [
  {
    key: "normal",
    label: "正常执行",
    description: "T01 已验收，其余任务按静态依赖等待派发。",
  },
  {
    key: "blocked",
    label: "阻塞待处理",
    description: "T01 处于 blocked，后续任务保留等待。",
  },
  {
    key: "awaiting_review",
    label: "独立复核",
    description: "T01 已提交，正在等待非作者复核。",
  },
  {
    key: "risk_accepted",
    label: "人工接受风险",
    description: "T01 仍为未验证，人工接受风险不能被显示为通过。",
  },
  {
    key: "stale",
    label: "改版失效",
    description: "T01 的验证证据已过期，需要重新验收。",
  },
  {
    key: "outcome_unknown",
    label: "结果未知",
    description: "T01 在途结果未知，不能自动重派。",
  },
  {
    key: "completed",
    label: "已完成",
    description: "全部必要任务通过独立检查并完成验收。",
  },
];

export const P6_PLAN = planBundle.plan;
export const P6_MANIFEST = {
  ...manifest,
  edgeCount: planBundle.edge_count,
};
export const P6_TRACEABILITY = traceability;
export const P6_SCENARIOS: Record<P6ScenarioKey, P6Scenario> =
  Object.fromEntries(
    scenarioMeta.map((scenario) => [
      scenario.key,
      {
        ...scenario,
        run: parseSnapshot(
          structuredClone(scenarioFixtures[scenario.key]),
        ),
      },
    ]),
  ) as Record<P6ScenarioKey, P6Scenario>;

export interface P6TaskView {
  task: Task;
  runtime: TaskRuntime;
  projection?: ProjectionTask;
}

export function buildTaskViews(run: Run, plan: Plan = P6_PLAN): P6TaskView[] {
  const runtimeById = new Map(
    run.tasks.map((runtime) => [runtime.task_id, runtime]),
  );
  return plan.tasks.map((task) => ({
    task,
    runtime:
      runtimeById.get(task.task_id) ??
      ({
        schema_version: "1.0",
        task_id: task.task_id,
        state: "waiting",
        verification_status: "unverified",
        disposition: "none",
        disposition_reason: null,
        generation: 1,
      } satisfies TaskRuntime),
  }));
}

export type P6StageStatus =
  | "accepted"
  | "in_progress"
  | "waiting"
  | "waiting_human"
  | "blocked";

export interface P6StageView {
  key: string;
  title: string;
  description: string;
  status: P6StageStatus;
}

export function deriveStages(run: Run, tasks: P6TaskView[]): P6StageView[] {
  const approved = new Set(
    run.approved_package.map((reference) => reference.object_id),
  );
  const hasBlocked = tasks.some(({ runtime }) =>
    ["blocked", "failed"].includes(runtime.state),
  );
  const hasActive = tasks.some(({ runtime }) =>
    ["running", "verifying", "ready"].includes(runtime.state),
  );
  const allAccepted = tasks.every(({ runtime }) => runtime.state === "accepted");
  const complete = run.status === "completed" || allAccepted;

  const executionStatus: P6StageStatus =
    run.status === "completed"
      ? "accepted"
      : hasBlocked
        ? "blocked"
        : hasActive
          ? "in_progress"
          : run.status === "paused"
            ? "waiting_human"
            : "waiting";

  const acceptanceStatus: P6StageStatus =
    run.status === "completed"
      ? "accepted"
      : run.status === "awaiting_confirmation" || allAccepted
        ? "waiting_human"
        : "waiting";

  return [
    {
      key: "requirement",
      title: "需求确认",
      description: approved.has("I001") ? "REQ-I001 · v2" : "等待需求确认",
      status: approved.has("I001") ? "accepted" : "waiting",
    },
    {
      key: "design",
      title: "设计规格",
      description: approved.has("DS-I001") ? "DS-I001 · v1" : "等待 Athena",
      status: approved.has("DS-I001") ? "accepted" : "waiting",
    },
    {
      key: "plan",
      title: "计划确认",
      description: approved.has("PLAN-I001")
        ? `PLAN-I001 · v${P6_PLAN.version}`
        : "等待计划确认",
      status: approved.has("PLAN-I001") ? "accepted" : "waiting",
    },
    {
      key: "execute",
      title: "受控执行",
      description: hasBlocked
        ? "存在阻塞任务，等待人工处理"
        : hasActive
          ? `${tasks.filter(({ runtime }) => runtime.state === "accepted").length} / ${tasks.length} 已验收`
          : run.status === "paused"
            ? "已暂停，在途结果待收尾"
            : "等待调度器派发",
      status: executionStatus,
    },
    {
      key: "acceptance",
      title: "最终验收",
      description:
        acceptanceStatus === "accepted"
          ? "交付已确认"
          : acceptanceStatus === "waiting_human"
            ? "等待真人确认"
            : complete
              ? "准备最终确认"
              : "等待执行结果",
      status: acceptanceStatus,
    },
  ];
}

export type P6CoverageStatus =
  | "verified"
  | "unverified"
  | "risk_accepted"
  | "in_progress"
  | "pending";

export interface P6TraceRow extends P6TraceFixtureRow {
  coverage: P6CoverageStatus;
  coverageLabel: string;
  taskStates: TaskRuntime["state"][];
}

const coverageLabels: Record<P6CoverageStatus, string> = {
  verified: "已验证",
  unverified: "未验证",
  risk_accepted: "未验证 · 人工接受",
  in_progress: "进行中",
  pending: "待处理",
};

export function deriveTraceRows(
  tasks: P6TaskView[],
  rows: P6TraceFixtureRow[] = traceability.rows,
): P6TraceRow[] {
  const runtimeById = new Map(
    tasks.map(({ task, runtime }) => [task.task_id, runtime]),
  );
  return rows.map((row) => {
    const runtimes = row.all_tasks
      .map((taskId) => runtimeById.get(taskId))
      .filter((runtime): runtime is TaskRuntime => Boolean(runtime));
    const states = runtimes.map((runtime) => runtime.state);
    let coverage: P6CoverageStatus = "pending";

    if (runtimes.some((runtime) => runtime.disposition === "risk_accepted")) {
      coverage = "risk_accepted";
    } else if (
      runtimes.length > 0 &&
      runtimes.every(
        (runtime) =>
          runtime.state === "accepted" &&
          runtime.verification_status === "passed",
      )
    ) {
      coverage = "verified";
    } else if (states.some((state) => ["blocked", "failed"].includes(state))) {
      coverage = "unverified";
    } else if (
      states.some((state) => ["running", "verifying", "ready"].includes(state))
    ) {
      coverage = "in_progress";
    }

    return {
      ...row,
      coverage,
      coverageLabel: coverageLabels[coverage],
      taskStates: states,
    };
  });
}

export interface P6Lane {
  agent: string;
  tasks: P6TaskView[];
}

export function buildLanes(tasks: P6TaskView[]): P6Lane[] {
  const grouped = new Map<string, P6TaskView[]>();
  for (const taskView of tasks) {
    const agent = taskView.task.executor;
    grouped.set(agent, [...(grouped.get(agent) ?? []), taskView]);
  }
  return [...grouped.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([agent, agentTasks]) => ({
      agent,
      tasks: agentTasks.sort(
        (left, right) =>
          left.task.priority - right.task.priority ||
          left.task.task_id.localeCompare(right.task.task_id),
      ),
    }));
}

export interface P6DagLayout {
  width: number;
  height: number;
  positions: Map<string, { x: number; y: number }>;
  criticalPairs: Set<string>;
}

const NODE_WIDTH = 176;
const NODE_HEIGHT = 62;
const COLUMN_GAP = 64;
const ROW_GAP = 64;

export function buildDagLayout(): P6DagLayout {
  const taskById = new Map(P6_PLAN.tasks.map((task) => [task.task_id, task]));
  const layers = manifest.layers.map((layer) =>
    layer.filter((taskId) => taskById.has(taskId)),
  );
  const maxRows = Math.max(...layers.map((layer) => layer.length), 1);
  const height = Math.max(
    250,
    maxRows * NODE_HEIGHT + (maxRows - 1) * ROW_GAP + 50,
  );
  const width = layers.length * NODE_WIDTH + (layers.length - 1) * COLUMN_GAP + 60;
  const positions = new Map<string, { x: number; y: number }>();

  layers.forEach((layer, layerIndex) => {
    const layerHeight =
      layer.length * NODE_HEIGHT + Math.max(0, layer.length - 1) * ROW_GAP;
    const startY = (height - layerHeight) / 2;
    layer.forEach((taskId, rowIndex) => {
      positions.set(taskId, {
        x: 30 + layerIndex * (NODE_WIDTH + COLUMN_GAP),
        y: startY + rowIndex * (NODE_HEIGHT + ROW_GAP),
      });
    });
  });

  const criticalPairs = new Set(
    manifest.critical_path.critical_chain
      .slice(0, -1)
      .map(
        (taskId, index) =>
          `${taskId}->${manifest.critical_path.critical_chain[index + 1]}`,
      ),
  );

  return { width, height, positions, criticalPairs };
}

export const P6_NODE_SIZE = {
  width: NODE_WIDTH,
  height: NODE_HEIGHT,
};

export type P6CommandAction = Command["action"];

export interface P6CommandDefinition {
  action: P6CommandAction;
  label: string;
  description: string;
  reasonRequired?: boolean;
}

export const P6_COMMANDS: P6CommandDefinition[] = [
  {
    action: "approve_design",
    label: "批准设计",
    description: "在 draft 状态按当前内容版本确认设计规格。",
  },
  {
    action: "approve_plan",
    label: "批准计划",
    description: "在 draft 状态按当前内容版本确认任务计划。",
  },
  {
    action: "start",
    label: "启动运行",
    description: "确认计划后进入确定性调度。",
  },
  {
    action: "pause",
    label: "暂停运行",
    description: "停止新派发，等待在途调用收尾。",
  },
  {
    action: "resume",
    label: "恢复运行",
    description: "从暂停状态恢复确定性调度。",
  },
  {
    action: "retry",
    label: "重试任务",
    description: "仅对 failed 或 blocked 任务提交人工重试。",
  },
  {
    action: "reassign",
    label: "改派执行者",
    description: "改派后仍需通过相同的独立验收。",
  },
  {
    action: "accept_risk",
    label: "接受剩余风险",
    description: "保留未验证与人工接受标记，不标为通过。",
    reasonRequired: true,
  },
  {
    action: "request_change",
    label: "请求变更",
    description: "记录变更原因，由后端按影响层级回退。",
    reasonRequired: true,
  },
  {
    action: "confirm_delivery",
    label: "确认交付",
    description: "仅在真人确认关口提交当前版本。",
  },
];

export function commandTargets(
  action: P6CommandAction,
  run: Run,
  tasks: P6TaskView[],
): Array<{ value: string; label: string }> {
  const blocked = tasks.filter(({ runtime }) =>
    ["blocked", "failed"].includes(runtime.state),
  );
  if (action === "retry" || action === "accept_risk") {
    return blocked.map(({ task, runtime }) => ({
      value: task.task_id,
      label: `${task.task_id} · ${task.goal} · ${runtime.state}`,
    }));
  }
  if (action === "reassign") {
    return tasks
      .filter(({ runtime }) =>
        ["blocked", "failed", "ready", "waiting"].includes(runtime.state),
      )
      .map(({ task, runtime }) => ({
        value: task.task_id,
        label: `${task.task_id} · ${task.goal} · ${runtime.state}`,
      }));
  }
  return [{ value: run.run_id, label: `${run.run_id} · 当前运行` }];
}

export function commandAvailability(
  action: P6CommandAction,
  run: Run,
  tasks: P6TaskView[],
): string | null {
  const hasBlocked = tasks.some(({ runtime }) =>
    ["blocked", "failed"].includes(runtime.state),
  );
  const allAccepted = tasks.every(
    ({ runtime }) => runtime.state === "accepted",
  );

  if (
    ["approve_design", "approve_plan", "start"].includes(action) &&
    run.status !== "draft"
  ) {
    return "仅 draft 状态可用";
  }
  if (action === "pause" && run.status !== "active") {
    return "仅 active 状态可用";
  }
  if (action === "resume" && run.status !== "paused") {
    return "仅 paused 状态可用";
  }
  if (action === "retry" && (!hasBlocked || !["active", "paused"].includes(run.status))) {
    return "当前没有可重试的阻塞任务";
  }
  if (
    action === "reassign" &&
    (!commandTargets(action, run, tasks).length ||
      !["draft", "active", "paused"].includes(run.status))
  ) {
    return "当前没有可改派任务";
  }
  if (
    action === "accept_risk" &&
    (!hasBlocked || !["active", "paused"].includes(run.status))
  ) {
    return "当前没有可接受风险的阻塞任务";
  }
  if (
    action === "request_change" &&
    !["active", "paused"].includes(run.status)
  ) {
    return "仅 active 或 paused 状态可用";
  }
  if (
    action === "confirm_delivery" &&
    (!allAccepted || !["active", "awaiting_confirmation"].includes(run.status))
  ) {
    return "必要任务尚未全部验收";
  }
  return null;
}

export function createCommand(
  action: P6CommandAction,
  run: Run,
  targetId: string,
  reason: string,
): Command {
  const operationId =
    globalThis.crypto?.randomUUID?.() ??
    `P6-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return {
    schema_version: "1.0",
    operation_id: operationId,
    run_id: run.run_id,
    actor: "YOU",
    action,
    target_id: targetId,
    expected_version: P6_PLAN.version,
    expected_state_revision: run.state_revision,
    parameters: reason.trim() ? { note: reason.trim() } : {},
  };
}

export function formatTaskState(state: TaskRuntime["state"]): string {
  return {
    waiting: "等待",
    ready: "就绪",
    running: "运行中",
    verifying: "待复核",
    accepted: "已验收",
    failed: "失败",
    blocked: "阻塞",
    cancelled: "已取消",
  }[state];
}

export function formatVerification(
  status: TaskRuntime["verification_status"],
): string {
  return {
    unverified: "未验证",
    passed: "检查通过",
    failed: "检查失败",
    stale: "证据已过期",
  }[status];
}

export function formatRunStatus(run: Run): string {
  return {
    draft: "草稿",
    active: "运行中",
    paused: "已暂停",
    awaiting_confirmation: "等待确认",
    completed: "已完成",
  }[run.status];
}

function syntheticTaskForProjection(
  view: RunView,
  projectionTask: ProjectionTask,
): Task {
  const repair = view.repairs.find(
    (entry) => entry.repair_task_id === projectionTask.task_id,
  ) as
    | {
        repair_task_id?: string;
        supersedes?: string;
        failure_ref?: string;
        reason?: string;
      }
    | undefined;
  return {
    schema_version: "1.0",
    task_id: projectionTask.task_id,
    version: view.trace?.plan_ref.version ?? P6_PLAN.version,
    content_hash: `dynamic-${projectionTask.task_id}`,
    type: projectionTask.type,
    goal: repair?.supersedes
      ? `修复 ${repair.supersedes}`
      : `动态任务 ${projectionTask.task_id}`,
    sources: (view.trace?.task_to_sources[projectionTask.task_id] ??
      [repair?.failure_ref ?? "REPAIR"]) as [string, ...string[]],
    inputs: [],
    outputs: [`artifact:${projectionTask.task_id}`],
    depends_on: repair?.supersedes ? [repair.supersedes] : [],
    executor: projectionTask.executor,
    reviewer: projectionTask.reviewer,
    write_scope: [`dynamic/${projectionTask.task_id}`],
    checks: [`check:${projectionTask.task_id}`],
    resources: [],
    priority: projectionTask.priority_rank,
    policy: {
      schema_version: "1.0",
      max_auto_retries: 2,
      timeout_seconds: 120,
    },
    required: projectionTask.required,
  };
}

export function buildTaskViewsFromView(view: RunView): P6TaskView[] {
  const planById = new Map(
    P6_PLAN.tasks.map((task) => [task.task_id, task]),
  );
  const runtimeById = new Map(
    view.snapshot.tasks.map((runtime) => [runtime.task_id, runtime]),
  );
  return [...view.projection.tasks]
    .sort(
      (left, right) =>
        left.priority_rank - right.priority_rank ||
        left.task_id.localeCompare(right.task_id),
    )
    .map((projection) => {
      const task =
        planById.get(projection.task_id) ??
        syntheticTaskForProjection(view, projection);
      const runtime =
        runtimeById.get(projection.task_id) ??
        ({
          schema_version: "1.0",
          task_id: projection.task_id,
          state: projection.state,
          verification_status: projection.verification,
          disposition: "none",
          disposition_reason: null,
          generation: projection.generation,
        } satisfies TaskRuntime);
      return { task, runtime, projection };
    });
}

export function deriveStagesFromView(
  view: RunView,
  tasks: P6TaskView[],
): P6StageView[] {
  const run = view.snapshot;
  const approved = new Set(
    run.approved_package.map((reference) => reference.object_id),
  );
  const progress = view.projection.progress;
  const blocked = view.projection.sets.blocked.length > 0;
  const active =
    tasks.some(({ runtime }) =>
      ["running", "verifying", "ready"].includes(runtime.state),
    ) || view.projection.resources.inflight > 0;
  const complete =
    run.status === "completed" ||
    (progress.required > 0 && progress.accepted >= progress.required);

  const executionStatus: P6StageStatus =
    run.status === "completed"
      ? "accepted"
      : blocked
        ? "blocked"
        : active
          ? "in_progress"
          : run.status === "paused"
            ? "waiting_human"
            : "waiting";
  const acceptanceStatus: P6StageStatus =
    run.status === "completed"
      ? "accepted"
      : run.status === "awaiting_confirmation" || complete
        ? "waiting_human"
        : "waiting";

  return [
    {
      key: "requirement",
      title: "需求确认",
      description: approved.has("I001")
        ? "REQ-I001 · 已批准"
        : "等待需求确认",
      status: approved.has("I001") ? "accepted" : "waiting",
    },
    {
      key: "design",
      title: "设计规格",
      description: approved.has("DS-I001")
        ? "DS-I001 · 已批准"
        : "等待 Athena",
      status: approved.has("DS-I001") ? "accepted" : "waiting",
    },
    {
      key: "plan",
      title: "计划确认",
      description: view.trace
        ? `${view.trace.plan_ref.object_id} · v${view.trace.plan_ref.version}`
        : "等待计划确认",
      status: approved.has("PLAN-I001") ? "accepted" : "waiting",
    },
    {
      key: "execute",
      title: "受控执行",
      description: blocked
        ? view.projection.sets.blocked
            .map((item) => `${item.task_id}: ${item.why}`)
            .join("；")
        : `${progress.accepted} / ${progress.required} 已验收`,
      status: executionStatus,
    },
    {
      key: "acceptance",
      title: "最终验收",
      description:
        acceptanceStatus === "accepted"
          ? "交付已确认"
          : acceptanceStatus === "waiting_human"
            ? "等待真人确认"
            : "等待执行结果",
      status: acceptanceStatus,
    },
  ];
}

export function deriveTraceRowsFromView(
  view: RunView,
  tasks: P6TaskView[],
): P6TraceRow[] {
  if (!view.trace) return [];
  const runtimeById = new Map(
    tasks.map(({ task, runtime }) => [task.task_id, runtime]),
  );
  return view.trace.rows.map((row) => {
    const runtimes = row.all_tasks
      .map((taskId) => runtimeById.get(taskId))
      .filter((runtime): runtime is TaskRuntime => Boolean(runtime));
    const states = runtimes.map((runtime) => runtime.state);
    let coverage: P6CoverageStatus = "pending";

    if (runtimes.some((runtime) => runtime.disposition === "risk_accepted")) {
      coverage = "risk_accepted";
    } else if (
      row.accepted &&
      row.verification_status === "passed"
    ) {
      coverage = "verified";
    } else if (states.some((state) => ["blocked", "failed"].includes(state))) {
      coverage = "unverified";
    } else if (
      states.some((state) => ["running", "verifying", "ready"].includes(state))
    ) {
      coverage = "in_progress";
    }
    return {
      ...row,
      coverage,
      coverageLabel: coverageLabels[coverage],
      taskStates: states,
    };
  });
}

export function buildDagLayoutFromView(view: RunView): P6DagLayout {
  const layers = view.projection.manifest.layers;
  const maxRows = Math.max(...layers.map((layer) => layer.length), 1);
  const height = Math.max(
    250,
    maxRows * NODE_HEIGHT + (maxRows - 1) * ROW_GAP + 50,
  );
  const width =
    layers.length * NODE_WIDTH + Math.max(0, layers.length - 1) * COLUMN_GAP + 60;
  const positions = new Map<string, { x: number; y: number }>();
  layers.forEach((layer, layerIndex) => {
    const layerHeight =
      layer.length * NODE_HEIGHT + Math.max(0, layer.length - 1) * ROW_GAP;
    const startY = (height - layerHeight) / 2;
    layer.forEach((taskId, rowIndex) => {
      positions.set(taskId, {
        x: 30 + layerIndex * (NODE_WIDTH + COLUMN_GAP),
        y: startY + rowIndex * (NODE_HEIGHT + ROW_GAP),
      });
    });
  });
  const chain = view.projection.manifest.critical_chain;
  return {
    width,
    height,
    positions,
    criticalPairs: new Set(
      chain
        .slice(0, -1)
        .map((taskId, index) => `${taskId}->${chain[index + 1]}`),
    ),
  };
}

export function allowedActionFor(
  view: RunView,
  action: P6CommandAction,
): AllowedAction | undefined {
  return view.allowed_actions.find((item) => item.action === action);
}

export function commandAvailabilityFromView(
  view: RunView,
  action: P6CommandAction,
): string | null {
  const allowed = allowedActionFor(view, action);
  if (!allowed) return "后端没有声明该命令";
  if (allowed.enabled) return null;
  return allowed.reasons.join("；") || "当前状态不允许该命令";
}

export interface P6CommandPayload {
  reason?: string;
  prompt?: string;
  assignee?: string;
  level?: "task" | "design" | "requirement";
  summary?: string;
}

export function createCommandForView(
  action: P6CommandAction,
  view: RunView,
  targetId: string,
  payload: P6CommandPayload,
): Command {
  const operationId =
    globalThis.crypto?.randomUUID?.() ??
    `P6-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const parameters: Record<string, unknown> = {};
  if (payload.prompt?.trim()) parameters.prompt = payload.prompt.trim();
  if (payload.reason?.trim()) parameters.reason = payload.reason.trim();
  if (payload.assignee?.trim()) parameters.assignee = payload.assignee.trim();
  if (payload.level) parameters.level = payload.level;
  if (payload.summary?.trim()) parameters.summary = payload.summary.trim();
  return {
    schema_version: "1.0",
    operation_id: operationId,
    run_id: view.snapshot.run_id,
    actor: "YOU",
    action,
    target_id: targetId,
    expected_version:
      view.trace?.plan_ref.version ??
      view.snapshot.approved_package.find((item) =>
        item.object_id.startsWith("PLAN"),
      )?.version ??
      P6_PLAN.version,
    expected_state_revision: view.snapshot.state_revision,
    parameters,
  };
}
