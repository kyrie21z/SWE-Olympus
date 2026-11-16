import { describe, expect, it } from "vitest";
import viewFixture from "../../../fixtures/http/view.json";
import { parseSnapshot } from "../../src/data/core/events";
import type { RunView } from "../../src/data/core/run-view";
import {
  P6_COMMANDS,
  allowedActionFor,
  buildDagLayoutFromView,
  buildTaskViewsFromView,
  commandAvailabilityFromView,
  createCommandForView,
  deriveStagesFromView,
  deriveTraceRowsFromView,
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
