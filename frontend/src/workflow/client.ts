import type { Design, Plan, Requirement } from "../contracts/generated";

export const AGENT_ROLES = ["Athena", "Daedalus", "Momus", "Hephaestus", "Argus", "Hermes"] as const;
export type AgentRole = (typeof AGENT_ROLES)[number];
export interface AgentSetting {
  role_id: AgentRole;
  base_url: string;
  model: string;
  timeout_seconds: number;
  configured: boolean;
  has_api_key: boolean;
}
export interface AgentSettingUpdate {
  base_url: string;
  model: string;
  timeout_seconds: number;
  api_key?: string;
  clear_api_key?: boolean;
}
export type WorkflowStage =
  | "requirement_pending" | "requirement_approved" | "design_generating"
  | "design_review_blocked" | "design_pending_approval" | "design_approved"
  | "plan_generating" | "plan_review_blocked" | "plan_pending_approval"
  | "plan_approved" | "executing" | "execution_blocked"
  | "awaiting_acceptance" | "completed" | "error";
export type WorkflowAction = "approve_requirement" | "generate_design" | "approve_design"
  | "generate_plan" | "approve_plan" | "execute" | "accept_run";
export type WorkflowModelPreference = "auto" | "offline" | "online";
export interface WorkflowSnapshot {
  workflow_id: string;
  run_id: string;
  stage: WorkflowStage;
  model_mode?: "offline" | "online";
  plan_bound?: boolean;
  offline_supported?: boolean;
  pending_model_role?: AgentRole | null;
  updated_at?: string;
  design_revision?: { round: number; limit: number; started_at: string; finding_count: number };
  error: { code: string; message: string; action?: WorkflowAction | null } | null;
  requirement: Requirement;
  design: Design | null;
  design_review: unknown;
  design_review_history?: unknown[];
  plan: Plan | null;
  plan_review: unknown;
  plan_review_record?: unknown;
  acceptance?: Record<string, unknown>;
  executions: unknown[] | Record<string, unknown>;
  retry_action?: WorkflowAction | null;
  retry_safe?: boolean;
  allowed_actions?: WorkflowAction[];
  [key: string]: unknown;
}

export class WorkflowApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number) {
    super(message);
    this.name = "WorkflowApiError";
  }
}

export class WorkflowClient {
  constructor(private readonly baseUrl = "") {}

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? {"Content-Type": "application/json"} : {}), ...init?.headers },
    });
    if (!response.ok) {
      let body: {error?: {code?: string; message?: string}} = {};
      try { body = await response.json(); } catch { /* HTTP status remains useful. */ }
      throw new WorkflowApiError(body.error?.code ?? `HTTP_${response.status}`, body.error?.message ?? "后端请求失败", response.status);
    }
    return await response.json() as T;
  }

  getAgents(signal?: AbortSignal): Promise<{agents: AgentSetting[]}> {
    return this.request("/api/settings/agents", {signal});
  }
  saveAgent(role: AgentRole, update: AgentSettingUpdate): Promise<AgentSetting> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}`, {method: "PUT", body: JSON.stringify(update)});
  }
  testAgent(role: AgentRole): Promise<{ok: boolean; message: string; model?: string; elapsed_ms?: number | null}> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}/test`, {method: "POST"});
  }
  listModels(role: AgentRole, baseUrl: string, key: string): Promise<{models: string[]}> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}/models`, {
      method: "POST", signal: AbortSignal.timeout(15_000),
      body: JSON.stringify({base_url: baseUrl.trim(), ...(key.trim() ? {api_key: key.trim()} : {})}),
    });
  }
  create(requirement: Requirement, allowSynthetic: boolean, modelMode: WorkflowModelPreference = "auto"): Promise<WorkflowSnapshot> {
    return this.request("/api/workflows", {method: "POST", body: JSON.stringify({requirement, allow_synthetic: allowSynthetic, model_mode: modelMode})});
  }
  get(id: string, signal?: AbortSignal): Promise<WorkflowSnapshot> {
    const timeout = AbortSignal.timeout(12_000);
    return this.request(`/api/workflows/${encodeURIComponent(id)}`, {signal: signal ? AbortSignal.any([signal, timeout]) : timeout});
  }
  action(id: string, action: WorkflowAction, operationId: string): Promise<WorkflowSnapshot> {
    return this.request(`/api/workflows/${encodeURIComponent(id)}/actions`, {method: "POST", body: JSON.stringify({action, operation_id: operationId})});
  }
}

export const workflowClient = new WorkflowClient(import.meta.env?.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "");

export function describeApiError(error: unknown): string {
  if (error instanceof WorkflowApiError) return `${error.code}：${error.message}`;
  if (error instanceof DOMException && error.name === "TimeoutError") return "请求超时，请稍后刷新。";
  return error instanceof Error ? error.message : "无法连接后端，请检查服务是否启动。";
}
