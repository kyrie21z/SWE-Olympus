import type { AgentSettingUpdate, WorkflowAction, WorkflowSnapshot, WorkflowStage } from "./client";

export const STAGES: Record<WorkflowStage, string> = {
  requirement_pending: "等待需求接纳", requirement_approved: "需求已接纳",
  design_generating: "Athena 正在生成设计并由 Momus 审查", design_review_blocked: "设计审查阻断",
  design_pending_approval: "等待批准设计", design_approved: "设计已批准",
  plan_generating: "Daedalus 正在规划并冻结验收检查", plan_review_blocked: "计划审查阻断",
  plan_pending_approval: "等待批准计划与验收检查", plan_approved: "计划已批准",
  executing: "正在执行任务并独立复核", execution_blocked: "执行受阻",
  awaiting_acceptance: "等待最终验收", completed: "已完成", error: "运行出错",
};
export const BUSY_STAGES = new Set<WorkflowStage>(["design_generating", "plan_generating", "executing"]);
export const NEXT_ACTION: Partial<Record<WorkflowStage, {action: WorkflowAction; label: string; tab: string}>> = {
  requirement_pending: {action: "approve_requirement", label: "接纳此需求用于当前运行", tab: "requirement"},
  requirement_approved: {action: "generate_design", label: "生成设计规格", tab: "design"},
  design_review_blocked: {action: "generate_design", label: "复核意见并继续修订", tab: "design"},
  design_pending_approval: {action: "approve_design", label: "批准当前设计", tab: "design"},
  design_approved: {action: "generate_plan", label: "生成任务计划", tab: "plan"},
  plan_review_blocked: {action: "generate_plan", label: "按审查意见修订计划", tab: "plan"},
  plan_pending_approval: {action: "approve_plan", label: "批准计划与验收检查", tab: "plan"},
  plan_approved: {action: "execute", label: "执行全部任务", tab: "execution"},
  execution_blocked: {action: "execute", label: "继续执行", tab: "execution"},
  awaiting_acceptance: {action: "accept_run", label: "确认最终验收", tab: "acceptance"},
};
export function stageIndex(stage: WorkflowStage): number {
  if (stage.startsWith("requirement")) return 0;
  if (stage.startsWith("design")) return 1;
  if (stage.startsWith("plan")) return 2;
  if (stage === "executing" || stage === "execution_blocked") return 3;
  return stage === "error" ? 0 : 4;
}
export function workflowStageIndex(snapshot: WorkflowSnapshot): number {
  if (snapshot.stage !== "error") return stageIndex(snapshot.stage);
  const action = snapshot.error?.action ?? snapshot.retry_action;
  if (action === "generate_design" || action === "approve_design") return 1;
  if (action === "generate_plan" || action === "approve_plan") return 2;
  if (action === "execute") return 3;
  if (action === "accept_run") return 4;
  if (snapshot.plan_bound) return 3;
  if (snapshot.design) return 1;
  return 0;
}
export function agentUpdate(baseUrl: string, model: string, timeout: number, key: string, clearKey: boolean): AgentSettingUpdate {
  return {base_url: baseUrl.trim(), model: model.trim(), timeout_seconds: timeout,
    ...(key.trim() ? {api_key: key.trim()} : {}), ...(clearKey ? {clear_api_key: true} : {})};
}
export function readableJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "暂无内容";
}
