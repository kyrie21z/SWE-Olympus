import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import schema from "../../../../docs/contracts/schema.json";
import type { Event, Run } from "../../contracts/generated";

const ajv = new Ajv2020({ strict: false });
addFormats(ajv);
ajv.addSchema(schema, "olympus");
const validateRun = ajv.compile({ $ref: "olympus#/$defs/Run" });
const validateEvent = ajv.compile({ $ref: "olympus#/$defs/Event" });

export function parseSnapshot(value: unknown): Run {
  if (!validateRun(value)) throw new Error("INVALID_SNAPSHOT");
  const run = value as Run;
  if (
    run.calls_used > run.call_budget ||
    new Set(run.tasks.map((t) => t.task_id)).size !== run.tasks.length
  )
    throw new Error("INVALID_RUN");
  for (const t of run.tasks) {
    if (t.state === "accepted" && t.verification_status !== "passed")
      throw new Error("UNVERIFIED_ACCEPTANCE");
    if (
      t.disposition === "risk_accepted" &&
      (!t.disposition_reason ||
        t.state === "accepted" ||
        t.verification_status === "passed")
    )
      throw new Error("RISK_IS_NOT_VERIFIED");
  }
  if (
    run.status === "completed" &&
    run.tasks.some((t) => t.state !== "accepted")
  )
    throw new Error("INVALID_COMPLETION");
  return structuredClone(run);
}

export interface MergeResult {
  snapshot: Run;
  needsRefetch: boolean;
  afterSeq: number;
  applied: number;
}

/** No guessed transitions: accept only contiguous authoritative event snapshots. */
export function mergeEvents(
  current: Run,
  incoming: readonly Event[],
): MergeResult {
  let snapshot = parseSnapshot(current);
  let applied = 0;
  const unique = new Map<number, Event>();
  for (const event of incoming) {
    if (!validateEvent(event) || event.run_id !== current.run_id)
      throw new Error("INVALID_EVENT");
    const projection = parseSnapshot(event.snapshot);
    if (
      projection.run_id !== event.run_id ||
      projection.event_seq !== event.event_seq ||
      projection.state_revision !== event.state_revision
    )
      throw new Error("EVENT_SNAPSHOT_MISMATCH");
    if (
      event.task_id &&
      projection.tasks.find((t) => t.task_id === event.task_id)?.state !==
        event.new_state
    )
      throw new Error("EVENT_STATE_MISMATCH");
    const existing = unique.get(event.event_seq);
    if (existing && JSON.stringify(existing) !== JSON.stringify(event))
      throw new Error("SEQUENCE_CONFLICT");
    unique.set(event.event_seq, event);
  }
  for (const event of [...unique.values()].sort(
    (a, b) => a.event_seq - b.event_seq,
  )) {
    if (event.event_seq <= snapshot.event_seq) continue;
    if (
      event.event_seq !== snapshot.event_seq + 1 ||
      event.state_revision !== snapshot.state_revision + 1
    )
      return {
        snapshot,
        needsRefetch: true,
        afterSeq: snapshot.event_seq,
        applied,
      };
    if (
      event.task_id &&
      snapshot.tasks.find((t) => t.task_id === event.task_id)?.state !==
        event.old_state
    )
      throw new Error("OLD_STATE_MISMATCH");
    snapshot = parseSnapshot(event.snapshot);
    applied++;
  }
  return {
    snapshot,
    needsRefetch: false,
    afterSeq: snapshot.event_seq,
    applied,
  };
}
