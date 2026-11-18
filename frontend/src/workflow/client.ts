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
  | "generate_plan" | "generate_plan_revision" | "approve_plan" | "execute" | "accept_run";
export type WorkflowModelPreference = "auto" | "offline" | "online";
export interface WorkflowSnapshot {
  workflow_id: string;
  run_id: string;
  successor_workflow_id?: string;
  successor_run_id?: string;
  source_workflow_id?: string;
  source_run_id?: string;
  stage: WorkflowStage;
  model_mode?: "offline" | "online";
  plan_bound?: boolean;
  offline_supported?: boolean;
  pending_model_role?: AgentRole | null;
  updated_at?: string;
  design_revision?: { round: number; limit: number; started_at: string; finding_count: number };
  error: { code: string; message: string; action?: WorkflowAction | null; role_id?: string; task_kind?: string; task_id?: string; error_code?: string; request_id?: string } | null;
  progress?: { role_id: string; task_kind: string; task_id?: string; completed?: number; total?: number; message: string } | null;
  pending_action?: WorkflowAction | null;
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
  constructor(private readonly baseUrl = "", private readonly requestTimeoutMs = 15_000) {}

  private async request<T>(path: string, init?: RequestInit, timeoutMs = this.requestTimeoutMs): Promise<T> {
    const controller = new AbortController();
    const externalSignal = init?.signal;
    const abort = () => controller.abort(externalSignal?.reason);
    if (externalSignal?.aborted) abort();
    else externalSignal?.addEventListener("abort", abort, {once: true});
    let timedOut = false;
    const timer = setTimeout(() => {timedOut = true; controller.abort();}, timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        ...init, signal: controller.signal,
        headers: { Accept: "application/json", ...(init?.body ? {"Content-Type": "application/json"} : {}), ...init?.headers },
      });
      if (!response.ok) {
        let body: {error?: {code?: string; message?: string}} = {};
        try { body = await response.json(); } catch { /* HTTP status remains useful. */ }
        if (controller.signal.aborted) throw controller.signal.reason;
        throw new WorkflowApiError(body.error?.code ?? `HTTP_${response.status}`, body.error?.message ?? "后端请求失败", response.status);
      }
      return await response.json() as T;
    } catch (error) {
      if (externalSignal?.aborted) throw error;
      if (timedOut) throw new WorkflowApiError("BACKEND_REQUEST_TIMEOUT", "后端响应超时；请求可能已被接收，请等待状态恢复后再操作。", 0);
      if (error instanceof TypeError) throw new WorkflowApiError("BACKEND_CONNECTION_LOST", "暂时无法连接后端，请检查服务和网络。", 0);
      throw error;
    } finally {
      clearTimeout(timer);
      externalSignal?.removeEventListener("abort", abort);
    }
  }

  getAgents(signal?: AbortSignal): Promise<{agents: AgentSetting[]}> {
    return this.request("/api/settings/agents", {signal});
  }
  saveAgent(role: AgentRole, update: AgentSettingUpdate): Promise<AgentSetting> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}`, {method: "PUT", body: JSON.stringify(update)});
  }
  testAgent(role: AgentRole, timeoutSeconds = 900): Promise<{ok: boolean; message: string; model?: string; elapsed_ms?: number | null}> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}/test`, {method: "POST"}, (timeoutSeconds + 5) * 1000);
  }
  listModels(role: AgentRole, baseUrl: string, key: string): Promise<{models: string[]}> {
    return this.request(`/api/settings/agents/${encodeURIComponent(role)}/models`, {
      method: "POST",
      body: JSON.stringify({base_url: baseUrl.trim(), ...(key.trim() ? {api_key: key.trim()} : {})}),
    });
  }
  create(requirement: Requirement, allowSynthetic: boolean, modelMode: WorkflowModelPreference = "auto"): Promise<WorkflowSnapshot> {
    return this.request("/api/workflows", {method: "POST", body: JSON.stringify({requirement, allow_synthetic: allowSynthetic, model_mode: modelMode})});
  }
  get(id: string, signal?: AbortSignal): Promise<WorkflowSnapshot> {
    return this.request(`/api/workflows/${encodeURIComponent(id)}`, {signal});
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

