import type { OlympusClient } from "../core/client";
import { parseSnapshot } from "../core/events";
import type { Command, Event, Receipt, Run } from "../../contracts/generated";

/** Isolated in-memory transport fixture. It never pretends to execute a task. */
export class MockClient implements OlympusClient {
  private readonly snapshot: Run;
  private readonly receipts = new Map<
    string,
    { input: string; receipt: Receipt }
  >();
  constructor(
    snapshot: Run,
    private readonly events: Event[] = [],
  ) {
    this.snapshot = parseSnapshot(snapshot);
    this.events = structuredClone(events);
  }
  async getSnapshot(runId: string) {
    if (runId !== this.snapshot.run_id) throw new Error("RUN_NOT_FOUND");
    return structuredClone(this.snapshot);
  }
  async getEvents(runId: string, afterSeq: number) {
    if (runId !== this.snapshot.run_id) throw new Error("RUN_NOT_FOUND");
    if (afterSeq < 0) throw new Error("INVALID_CURSOR");
    return structuredClone(
      this.events.filter((e) => e.run_id === runId && e.event_seq > afterSeq),
    );
  }
  async submit(command: Command): Promise<Receipt> {
    if (command.run_id !== this.snapshot.run_id)
      throw new Error("RUN_NOT_FOUND");
    const input = JSON.stringify(command);
    const previous = this.receipts.get(command.operation_id);
    if (previous) {
      if (previous.input !== input) throw new Error("IDEMPOTENCY_CONFLICT");
      return structuredClone(previous.receipt);
    }
    const receipt: Receipt = {
      schema_version: "1.0",
      operation_id: command.operation_id,
      run_id: command.run_id,
      status: "rejected",
      reason_code: "NOT_READY",
      state_revision: this.snapshot.state_revision,
    };
    this.receipts.set(command.operation_id, { input, receipt });
    return structuredClone(receipt);
  }
}
