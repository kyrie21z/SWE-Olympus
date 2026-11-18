import type {
  Command,
  Event,
  Receipt,
  Run,
} from "../../contracts/generated";
import type {
  CommandResult,
  ExecutionApiView,
  P6OlympusClient,
  RunView,
  TrailView,
} from "../core/run-view";

interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    detail?: unknown;
  };
}

export class HttpApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "HttpApiError";
  }
}

async function decodeError(response: Response): Promise<HttpApiError> {
  let body: ApiErrorEnvelope = {};
  try {
    body = (await response.json()) as ApiErrorEnvelope;
  } catch {
    // Keep the HTTP status as the only reliable evidence for non-JSON responses.
  }
  return new HttpApiError(
    response.status,
    body.error?.code ?? `HTTP_${response.status}`,
    body.error?.message ?? "请求失败",
    body.error?.detail,
  );
}

export class HttpOlympusClient implements P6OlympusClient {
  constructor(private readonly baseUrl = "") {}

  private async request<T>(path: string, init?: RequestInit): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    });
    if (!response.ok) {
      throw await decodeError(response);
    }
    return response;
  }

  async getSnapshot(runId: string): Promise<Run> {
    return (await (
      await this.request(`/api/runs/${encodeURIComponent(runId)}`)
    ).json()) as Run;
  }

  async getEvents(runId: string, afterSeq: number): Promise<Event[]> {
    return (await (
      await this.request(
        `/api/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`,
      )
    ).json()) as Event[];
  }

  async getView(runId: string): Promise<RunView> {
    return (await (
      await this.request(`/api/runs/${encodeURIComponent(runId)}/view`)
    ).json()) as RunView;
  }

  async getTrail(runId: string): Promise<TrailView> {
    return (await (
      await this.request(`/api/runs/${encodeURIComponent(runId)}/trail`)
    ).json()) as TrailView;
  }

  async getExecution(
    runId: string,
    taskId: string,
    attemptId?: string,
  ): Promise<ExecutionApiView> {
    const search = attemptId
      ? `?attempt_id=${encodeURIComponent(attemptId)}`
      : "";
    return (await (
      await this.request(
        `/api/runs/${encodeURIComponent(runId)}/executions/${encodeURIComponent(taskId)}${search}`,
      )
    ).json()) as ExecutionApiView;
  }

  async submit(command: Command): Promise<Receipt> {
    return (await this.submitDetailed(command)).receipt;
  }

  async submitDetailed(command: Command): Promise<CommandResult> {
    const response = await this.request("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command),
    });
    return {
      receipt: (await response.json()) as Receipt,
      durability:
        response.headers.get("X-Olympus-Durability") ?? "unknown",
      recordedEvents: Number(
        response.headers.get("X-Olympus-Recorded-Events") ?? "0",
      ),
    };
  }
}
