import { describe, it, expect } from "vitest";
import fixture from "../../../fixtures/contracts/valid.json";
import scenarios from "../../../fixtures/contracts/scenarios.json";
import { mergeEvents, parseSnapshot } from "../../src/data/core/events";
import { MockClient } from "../../src/data/mock/client";
import type { Command, Event } from "../../src/contracts/generated";

describe("authoritative event cache", () => {
  const run = parseSnapshot(fixture.run);
  const event = fixture.event as Event;
  it("deduplicates and leaves the original immutable", () => {
    const result = mergeEvents(run, [event, structuredClone(event)]);
    expect(result.applied).toBe(1);
    expect(result.snapshot.status).toBe("paused");
    expect(run.state_revision).toBe(0);
    expect(mergeEvents(result.snapshot, [event]).applied).toBe(0);
  });
  it("stops at gaps and requests from last committed sequence", () => {
    const later = structuredClone(event);
    later.event_seq = 3;
    later.state_revision = 3;
    later.snapshot.event_seq = 3;
    later.snapshot.state_revision = 3;
    const result = mergeEvents(run, [later]);
    expect(result.needsRefetch).toBe(true);
    expect(result.afterSeq).toBe(0);
    expect(result.snapshot).toEqual(run);
  });
  it("handles out-of-order contiguous batches", () => {
    const second = structuredClone(event);
    second.event_id = "EV-2";
    second.event_seq = 2;
    second.state_revision = 2;
    second.snapshot.event_seq = 2;
    second.snapshot.state_revision = 2;
    expect(mergeEvents(run, [second, event]).applied).toBe(2);
  });
  it("rejects sequence conflicts, cross-run events and invalid versions", () => {
    expect(() =>
      mergeEvents(run, [event, { ...event, event_id: "conflict" }]),
    ).toThrow("SEQUENCE_CONFLICT");
    expect(() => mergeEvents(run, [{ ...event, run_id: "other" }])).toThrow(
      "INVALID_EVENT",
    );
    expect(() => parseSnapshot({ ...run, schema_version: "0.9" })).toThrow(
      "INVALID_SNAPSHOT",
    );
  });
  it("keeps risk accepted, stale, unknown distinct from success", () => {
    for (const scenario of Object.values(scenarios))
      expect(parseSnapshot(scenario)).toBeTruthy();
    const risk = structuredClone(scenarios.risk_accepted);
    risk.tasks[0].state = "accepted";
    risk.tasks[0].verification_status = "passed";
    expect(() => parseSnapshot(risk)).toThrow("RISK_IS_NOT_VERIFIED");
  });
  it("mock responses cannot mutate state and do not execute commands", async () => {
    const client = new MockClient(run, [event]);
    const one = await client.getSnapshot(run.run_id);
    one.status = "paused";
    expect((await client.getSnapshot(run.run_id)).status).toBe("draft");
    const command = fixture.command as Command;
    const result = await client.submit(command);
    expect(result.reason_code).toBe("NOT_READY");
    expect(await client.submit(command)).toEqual(result);
    await expect(
      client.submit({ ...command, action: "resume" }),
    ).rejects.toThrow("IDEMPOTENCY_CONFLICT");
    expect(await client.getEvents(run.run_id, 1)).toEqual([]);
  });
});
