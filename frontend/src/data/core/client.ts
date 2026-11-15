import type { Command, Event, Receipt, Run } from "../../contracts/generated";

export interface OlympusClient {
  getSnapshot(runId: string): Promise<Run>;
  getEvents(runId: string, afterSeq: number): Promise<Event[]>;
  submit(command: Command): Promise<Receipt>;
}
