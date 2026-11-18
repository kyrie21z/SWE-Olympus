import type {
  Command,
  ExecutionRecord,
  Run,
  TaskRuntime,
} from "../../contracts/generated";

export interface AllowedAction {
  action: Command["action"];
  enabled: boolean;
  reasons: string[];
  actors: string[];
}

export interface ProjectionTask {
  task_id: string;
  type: "interface" | "implement" | "test" | "integrate" | "doc";
  state: TaskRuntime["state"];
  verification: TaskRuntime["verification_status"];
  generation: number;
  priority_rank: number;
  executor: string;
  reviewer: string;
  required: boolean;
  missing_predecessors: string[];
}

export interface RuntimeProjection {
  run: {
    run_id: string;
    status: Run["status"];
    state_revision: number;
    event_seq: number;
    call_budget: number;
    calls_used: number;
    tasks: Array<{
      task_id: string;
      state: TaskRuntime["state"];
      verification: TaskRuntime["verification_status"];
      disposition: TaskRuntime["disposition"];
      generation: number;
    }>;
  };
  manifest: {
    manifest_id: string;
    layers: string[][];
    topological_order: string[];
    critical_chain: string[];
  };
  sets: {
    eligible: string[];
    ready: string[];
    dispatchable: string[];
    blocked: Array<{ task_id: string; why: string }>;
  };
  progress: { accepted: number; required: number };
  pause_state: {
    paused: boolean;
    fully_stopped: boolean;
    unsettled: string[];
  };
  incidents: {
    open: Array<Record<string, unknown>>;
    resolved: Record<string, unknown>;
  };
  resources: {
    inflight: number;
    max_parallel: number;
    executor_capacity: number;
    executors: Record<string, number>;
    permits: Array<Record<string, unknown>>;
    attempts: Record<string, number>;
  };
  tasks: ProjectionTask[];
}

export interface TraceRow {
  req_id: string;
  req_text: string;
  ds_ids: string[];
  interface_tasks: string[];
  implement_tasks: string[];
  test_tasks: string[];
  integrate_tasks: string[];
  doc_tasks: string[];
  all_tasks: string[];
  stage_status: string;
  verification_status: string;
  accepted: boolean;
}

export interface TraceabilityIndex {
  schema_version: "1.0";
  index_id: string;
  req_ref: { object_id: string; version: number; content_hash: string };
  ds_ref: { object_id: string; version: number; content_hash: string };
  plan_ref: { object_id: string; version: number; content_hash: string };
  rows: TraceRow[];
  design_to_tasks: Record<string, string[]>;
  task_to_sources: Record<string, string[]>;
  uncovered_requirements: string[];
  uncovered_design_elements: string[];
  missing_test_requirements: string[];
  out_of_scope_hits: string[];
  dangling_sources: string[];
  coverage_complete: boolean;
  index_hash: string;
}

export interface RunView {
  schema_version: "1.0";
  run_id: string;
  snapshot: Run;
  projection: RuntimeProjection;
  allowed_actions: AllowedAction[];
  trace: TraceabilityIndex | null;
  repairs: Array<Record<string, unknown>>;
}

export interface TrailEntry {
  [key: string]: unknown;
}

export interface TrailView {
  run_id: string;
  assignments: Record<string, string>;
  entries: TrailEntry[];
  repairs: Array<Record<string, unknown>>;
}

export interface ExecutionApiView {
  run_id: string;
  task_id: string;
  attempt_id?: string;
  is_current: boolean;
  runtime: TaskRuntime;
  execution: ExecutionRecord & {
    attempt_id?: string;
    generation?: number;
    state?: string;
    tool_invocations?: number;
    tool_mode?: string;
    model_mode?: string;
    checks_passed?: string[];
    error?: string | null;
    artifacts?: unknown[];
    review?: unknown;
  };
  review_requests: Array<Record<string, unknown>>;
}

export interface CommandResult {
  receipt: import("../../contracts/generated").Receipt;
  durability: string;
  recordedEvents: number;
}

export interface P6OlympusClient {
  getSnapshot(runId: string): Promise<Run>;
  getEvents(runId: string, afterSeq: number): Promise<import("../../contracts/generated").Event[]>;
  submit(command: Command): Promise<import("../../contracts/generated").Receipt>;
  getView(runId: string): Promise<RunView>;
  getTrail(runId: string): Promise<TrailView>;
  getExecution(
    runId: string,
    taskId: string,
    attemptId?: string,
  ): Promise<ExecutionApiView>;
  submitDetailed(command: Command): Promise<CommandResult>;
}
