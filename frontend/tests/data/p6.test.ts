import { describe, expect, it } from "vitest";
import viewFixture from "../../../fixtures/http/view.json";
import { parseSnapshot } from "../../src/data/core/events";
import type { RunView } from "../../src/data/core/run-view";
import {
  P6_COMMANDS,
  P6_PLAN,
  allowedActionFor,
  buildDagLayoutFromView,
  buildTaskViewsFromView,
  commandAvailabilityFromView,
  createCommandForView,
  currentStageIndex,
  deriveStagesFromView,
  deriveTraceRowsFromView,
  executionWaits,
} from "../../src/p6/model";

function contractView(): RunView {
  const raw = structuredClone(viewFixture.body) as unknown as RunView;
  return { ...raw, snapshot: parseSnapshot(raw.snapshot) };
}

describe("P6 HTTP view projection", () => {
  it("builds every task from the server projection", () => {
    const view = contractView();
    const tasks = buildTaskViewsFromView(view);

    expect(tasks).toHaveLength(8);
    expect(tasks.map((item) => item.task.task_id)).toEqual([
      "T01",
      "T03",
      "T02",
      "T04",
      "T05",
      "T06",
      "T07",
      "T08",
    ]);
    expect(tasks[0].projection?.priority_rank).toBe(1);
    expect(tasks[0].runtime.state).toBe("waiting");
  });

  it("uses the server plan for reused task IDs and never borrows unrelated fixture semantics", () => {
    const view = contractView();
    view.plan = structuredClone(P6_PLAN);
    view.plan.object_id = "PLAN-inventory";
    const task = view.plan.tasks.find(item => item.task_id === "T01");
    if (!task) throw new Error("missing T01");
    task.goal = "实现库存预占接口";
    task.sources = ["FR-STOCK-001"];
    task.write_scope = ["inventory/core"];
    let tasks = buildTaskViewsFromView(view);
    expect(tasks.find(item => item.task.task_id === "T01")?.task).toMatchObject({goal:"实现库存预占接口",sources:["FR-STOCK-001"],write_scope:["inventory/core"]});
    const remaining = view.plan.tasks.filter(item => item.task_id !== "T01");
    if (!remaining.length) throw new Error("missing remaining plan tasks");
    view.plan.tasks = [remaining[0], ...remaining.slice(1)];
    tasks = buildTaskViewsFromView(view);
    expect(tasks.find(item => item.task.task_id === "T01")?.task.goal).toBe("动态任务 T01");
    expect(tasks.find(item => item.task.task_id === "T01")?.task.write_scope).toEqual(["dynamic/T01"]);
    delete view.plan;
    if (view.trace) view.trace.plan_ref.object_id = "PLAN-other-business";
    expect(buildTaskViewsFromView(view).find(item => item.task.task_id === "T01")?.task.goal).toBe("动态任务 T01");
  });

  it("uses backend progress, blocked sets, and pause state for stages", () => {
    const view = contractView();
    const tasks = buildTaskViewsFromView(view);
    const stages = deriveStagesFromView(view, tasks);

    expect(stages.find((stage) => stage.key === "execute")?.status).toBe(
      "waiting_human",
    );
    expect(
      stages.find((stage) => stage.key === "execute")?.description,
    ).toContain("0 / 8");
  });

  it("separates a ready task waiting for an executor from an actual failed task", () => {
    const view = contractView();
    view.snapshot.status = "active";
    const [running, queued] = view.snapshot.tasks;
    running.state = "running";
    queued.state = "ready";
    view.projection.sets.blocked = [{task_id: queued.task_id, why: ["执行者 Momus 已被占用"]}];
    const stage = () => deriveStagesFromView(view, buildTaskViewsFromView(view)).find(item => item.key === "execute");
    expect(stage()).toMatchObject({status: "in_progress", description: expect.stringContaining("1 项排队")});
    expect(executionWaits(view)).toEqual({blocked: [], queued: [{task_id: queued.task_id, why: "执行者 Momus 已被占用"}]});

    running.state = "failed";
    expect(stage()).toMatchObject({status: "blocked", description: `${running.task_id}: 任务执行或检查失败`});
    expect(executionWaits(view).blocked).toHaveLength(1);
  });

  it("binds stage approvals to arbitrary trace IDs, versions and fingerprints", () => {
    const view = contractView();
    if (!view.trace) throw new Error("missing trace");
    view.trace.req_ref = {object_id:"REQ-stock-control",version:3,content_hash:"a".repeat(64)};
    view.trace.ds_ref = {object_id:"DESIGN-storage-v2",version:5,content_hash:"b".repeat(64)};
    view.trace.plan_ref = {object_id:"PLAN-independent-07",version:2,content_hash:"c".repeat(64)};
    view.snapshot.approved_package = [view.trace.req_ref,view.trace.ds_ref,view.trace.plan_ref].map(ref => ({schema_version:"1.0",...ref}));
    let stages = deriveStagesFromView(view, buildTaskViewsFromView(view));
    expect(stages.slice(0,3).map(stage => stage.status)).toEqual(["accepted","accepted","accepted"]);
    expect(stages[0].description).toBe("REQ-stock-control · v3 · 已批准");
    expect(stages[1].description).toBe("DESIGN-storage-v2 · v5 · 已批准");
    expect(stages[2].description).toBe("PLAN-independent-07 · v2 · 已批准");
    view.snapshot.approved_package[1].version = 4;
    view.snapshot.approved_package[2].content_hash = "d".repeat(64);
    stages = deriveStagesFromView(view, buildTaskViewsFromView(view));
    expect(stages.slice(0,3).map(stage => stage.status)).toEqual(["accepted","waiting","waiting"]);
    expect(stages[1].description).toContain("v5 · 待批准");
  });

  it("keeps a completed run on final acceptance when all five stages are accepted", () => {
    const view = contractView();
    if (!view.trace) throw new Error("missing trace");
    view.snapshot.approved_package = [view.trace.req_ref,view.trace.ds_ref,view.trace.plan_ref].map(ref => ({schema_version:"1.0",...ref}));
    view.snapshot.status = "completed";
    view.snapshot.tasks.forEach(task => {task.state="accepted";task.verification_status="passed";});
    view.projection.progress.accepted = view.projection.progress.required;
    const stages = deriveStagesFromView(view, buildTaskViewsFromView(view));
    expect(stages.every(stage => stage.status === "accepted")).toBe(true);
    expect(stages[currentStageIndex(stages)].key).toBe("acceptance");
    expect(currentStageIndex([])).toBe(0);
  });

  it("keeps risk acceptance separate from verified traceability", () => {
    const view = contractView();
    view.snapshot = structuredClone(view.snapshot);
    const runtime = view.snapshot.tasks.find(
      (task) => task.task_id === "T01",
    );
    if (!runtime) throw new Error("missing T01");
    runtime.state = "cancelled";
    runtime.disposition = "risk_accepted";
    runtime.disposition_reason = "人工接受构造风险";
    const tasks = buildTaskViewsFromView(view);
    const trace = deriveTraceRowsFromView(view, tasks);

    expect(trace[0].coverage).toBe("risk_accepted");
    expect(trace[0].coverageLabel).toBe("未验证 · 人工接受");
  });

  it("reads DAG layers and critical chain from projection manifest", () => {
    const view = contractView();
    const layout = buildDagLayoutFromView(view);

    expect(layout.positions.size).toBe(8);
    expect(layout.criticalPairs.has("T01->T03")).toBe(true);
    expect(layout.criticalPairs.has("T02->T04")).toBe(false);
  });

  it("drives command availability from allowed_actions", () => {
    const view = contractView();
    const resume = allowedActionFor(view, "resume");
    const retry = allowedActionFor(view, "retry");

    expect(resume?.enabled).toBe(true);
    expect(commandAvailabilityFromView(view, "resume")).toBeNull();
    expect(retry?.enabled).toBe(false);
    expect(commandAvailabilityFromView(view, "retry")).toContain("没有");
  });

  it("maps request_change to backend parameter keys", () => {
    const view = contractView();
    const command = createCommandForView(
      "request_change",
      view,
      view.snapshot.run_id,
      {
        level: "design",
        summary: "设计接口需要调整",
      },
    );

    expect(command.action).toBe("request_change");
    expect(command.parameters).toEqual({
      level: "design",
      summary: "设计接口需要调整",
    });
    expect(command.expected_version).toBe(
      view.trace?.plan_ref.version ?? 1,
    );
    expect(P6_COMMANDS.some((item) => item.action === "request_change")).toBe(
      true,
    );
  });
});
