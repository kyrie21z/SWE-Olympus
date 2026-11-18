import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkflowApiError, WorkflowClient } from "../../src/workflow/client";
import { agentUpdate, NEXT_ACTION } from "../../src/workflow/model";
import requirement from "../../public/examples/requirement.json";
import type { Requirement } from "../../src/contracts/generated";

afterEach(() => vi.unstubAllGlobals());

describe("per-agent API configuration", () => {
  it("preserves a saved key when left empty and clears only with an explicit flag", () => {
    expect(agentUpdate(" https://example.test/v1 ", " model-a ", 90, " ", false)).toEqual({base_url:"https://example.test/v1",model:"model-a",timeout_seconds:90});
    expect(agentUpdate("https://example.test/v1", "model-a", 90, "", true)).toEqual({base_url:"https://example.test/v1",model:"model-a",timeout_seconds:90,clear_api_key:true});
    expect(agentUpdate("https://example.test/v1", "model-a", 90, " secret ", false).api_key).toBe("secret");
  });
  it("sends each role's changes only to that role and tests the saved configuration", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok:true,message:"connected"})));
    vi.stubGlobal("fetch", fetcher);
    const client = new WorkflowClient();
    await client.saveAgent("Athena", agentUpdate("https://example.test/v1", "a", 30, "key-a", false));
    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/settings/agents/Athena");
    expect(JSON.parse(init.body as string)).toMatchObject({api_key:"key-a",model:"a"});
    fetcher.mockResolvedValue(new Response(JSON.stringify({ok:true,message:"connected"})));
    await client.testAgent("Argus");
    expect(fetcher.mock.calls[1][0]).toBe("/api/settings/agents/Argus/test");
    expect(fetcher.mock.calls[1][1].body).toBeUndefined();
  });
});

describe("workflow HTTP lifecycle", () => {
  it("creates a run without sending any implicit approval", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({workflow_id:"WF-1",stage:"requirement_pending"})));
    vi.stubGlobal("fetch", fetcher);
    await new WorkflowClient().create(requirement as unknown as Requirement, true);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe("/api/workflows");
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({requirement,allow_synthetic:true,model_mode:"auto"});
  });
  it("keeps the operation ID supplied by the caller for safe resubmission", async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({stage:"design_generating"}))));
    vi.stubGlobal("fetch", fetcher);
    const client = new WorkflowClient();
    await client.action("WF / 1", "generate_design", "stable-operation");
    await client.action("WF / 1", "generate_design", "stable-operation");
    expect(fetcher.mock.calls[0][0]).toBe("/api/workflows/WF%20%2F%201/actions");
    expect(JSON.parse(fetcher.mock.calls[0][1].body).operation_id).toBe(JSON.parse(fetcher.mock.calls[1][1].body).operation_id);
  });
  it("surfaces structured backend error codes rather than fabricating a stage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:"OUTCOME_UNKNOWN",message:"query first"}}),{status:409})));
    const promise = new WorkflowClient().get("WF-1");
    await expect(promise).rejects.toBeInstanceOf(WorkflowApiError);
    await expect(promise).rejects.toMatchObject({code:"OUTCOME_UNKNOWN",status:409,message:"query first"});
  });
  it("requires separate human actions at requirement, design, plan and final acceptance gates", () => {
    expect(NEXT_ACTION.requirement_pending?.action).toBe("approve_requirement");
    expect(NEXT_ACTION.design_pending_approval?.action).toBe("approve_design");
    expect(NEXT_ACTION.plan_pending_approval?.action).toBe("approve_plan");
    expect(NEXT_ACTION.awaiting_acceptance?.action).toBe("accept_run");
    expect(NEXT_ACTION.design_generating).toBeUndefined();
    expect(NEXT_ACTION.error).toBeUndefined();
  });
});
