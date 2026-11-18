/* Generated from Python contracts via JSON Schema. Do not edit. */

export type SchemaVersion = "1.0";
export type SchemaVersion1 = "1.0";
export type ObjectId = string;
export type Version = number;
export type ContentHash = string;
/**
 * @minItems 1
 */
export type Provenance = [Provenance1, ...Provenance1[]];
export type SchemaVersion2 = "1.0";
export type Kind = "material" | "human" | "agent_hypothesis" | "synthetic_fixture";
export type SourceId = string;
export type Locator = string;
/**
 * @minItems 1
 */
export type Requirements = [RequirementItem, ...RequirementItem[]];
export type Id = string;
export type Text = string;
export type Acceptance = string;
export type OutOfScope = string[];
export type Kind1 = "REQ";
export type SchemaVersion3 = "1.0";
export type ConfirmationId = string;
export type Actor = string;
export type Origin = "human" | "synthetic_fixture";
export type SchemaVersion4 = "1.0";
export type ObjectId1 = string;
export type Version1 = number;
export type ContentHash1 = string;
export type Dependencies = Ref[];
export type ConfirmedAt = string;
export type SchemaVersion5 = "1.0";
export type ObjectId2 = string;
export type Version2 = number;
export type ContentHash2 = string;
/**
 * @minItems 1
 */
export type Provenance2 = [Provenance1, ...Provenance1[]];
/**
 * @minItems 1
 */
export type Modules = [string, ...string[]];
export type Data = string[];
export type Interfaces = string[];
export type States = string[];
export type Concurrency = string;
export type Exceptions = string[];
export type Interaction = string;
export type Decisions = string[];
/**
 * @minItems 1
 */
export type Elements = [DesignElement, ...DesignElement[]];
export type Id1 = string;
/**
 * @minItems 1
 */
export type Sources = [string, ...string[]];
export type Description = string;
export type AcceptanceMethod = string;
export type Kind2 = "DS";
export type SchemaVersion6 = "1.0";
export type ObjectId3 = string;
export type Version3 = number;
export type ContentHash3 = string;
/**
 * @minItems 1
 */
export type Provenance3 = [Provenance1, ...Provenance1[]];
export type JsonValue = unknown;
export type Kind3 = "PLAN";
/**
 * @minItems 1
 */
export type Tasks = [Task, ...Task[]];
export type SchemaVersion7 = "1.0";
export type TaskId = string;
export type Version4 = number;
export type ContentHash4 = string;
export type Type = "interface" | "implement" | "test" | "integrate" | "doc";
export type Goal = string;
/**
 * @minItems 1
 */
export type Sources1 = [string, ...string[]];
export type Inputs = string[];
/**
 * @minItems 1
 */
export type Outputs = [string, ...string[]];
export type DependsOn = string[];
export type Executor = string;
export type Reviewer = string;
/**
 * @minItems 1
 */
export type WriteScope = [string, ...string[]];
/**
 * @minItems 1
 */
export type Checks = [string, ...string[]];
export type Resources = string[];
export type Priority = number;
export type SchemaVersion8 = "1.0";
export type MaxAutoRetries = number;
export type TimeoutSeconds = number;
export type Required = boolean;
export type ApiContracts = {
  [k: string]: {
    [k: string]: string[];
  };
} | null;
export type SchemaVersion9 = "1.0";
export type RunId = string;
export type Mode = "mock" | "real_tool" | "real_model";
export type ApprovedPackage = Ref[];
export type StateRevision = number;
export type EventSeq = number;
export type Status = "draft" | "active" | "paused" | "awaiting_confirmation" | "completed";
export type CallBudget = number;
export type CallsUsed = number;
export type SchemaVersion10 = "1.0";
export type TaskId1 = string;
export type State = "waiting" | "ready" | "running" | "verifying" | "accepted" | "failed" | "blocked" | "cancelled";
export type VerificationStatus = "unverified" | "passed" | "failed" | "stale";
export type Disposition = "none" | "risk_accepted";
export type DispositionReason = string | null;
export type Generation = number;
export type Tasks1 = TaskRuntime[];
export type SchemaVersion11 = "1.0";
export type AttemptId = string;
export type LogicalTaskId = string;
export type RunId1 = string;
export type TaskId2 = string;
export type Generation1 = number;
export type InputHash = string;
export type ProviderRequestId = string | null;
export type IdempotencyKey = string;
export type Mode1 = "mock" | "real_tool" | "real_model";
export type Status1 = "prepared" | "sent" | "succeeded" | "failed" | "outcome_unknown";
export type AutoRetryCount = number;
export type HumanRetryCount = number;
export type SchemaVersion12 = "1.0";
export type ArtifactId = string;
export type RunId2 = string;
export type TaskId3 = string;
export type AttemptId1 = string;
export type Location = string;
export type ContentHash5 = string;
export type Inputs1 = Ref[];
export type ImplementationVersion = string;
export type TestVersion = string;
export type FixtureVersion = string;
export type Mode2 = "mock" | "real_tool" | "real_model";
export type SchemaVersion13 = "1.0";
export type ReviewId = string;
export type Author = string;
export type Reviewer1 = string;
export type Verdict = "passed" | "blocked" | "needs_revision";
export type Findings = string[];
/**
 * @minItems 1
 */
export type Evidence = [string, ...string[]];
export type Mode3 = "mock" | "real_tool" | "real_model";
export type SchemaVersion14 = "1.0";
export type EventId = string;
export type RunId3 = string;
export type EventSeq1 = number;
export type TaskId4 = string | null;
export type AttemptId2 = string | null;
export type OldState =
  ("waiting" | "ready" | "running" | "verifying" | "accepted" | "failed" | "blocked" | "cancelled") | null;
export type NewState =
  ("waiting" | "ready" | "running" | "verifying" | "accepted" | "failed" | "blocked" | "cancelled") | null;
export type Reason = string;
export type ActorType = "program" | "agent" | "human";
export type OccurredAt = string;
export type StateRevision1 = number;
export type SchemaVersion15 = "1.0";
export type OperationId = string;
export type RunId4 = string;
export type Actor1 = string;
export type Action =
  | "approve_design"
  | "approve_plan"
  | "start"
  | "pause"
  | "resume"
  | "retry"
  | "reassign"
  | "accept_risk"
  | "request_change"
  | "confirm_delivery";
export type TargetId = string;
export type ExpectedVersion = number;
export type ExpectedStateRevision = number;
export type SchemaVersion16 = "1.0";
export type OperationId1 = string;
export type RunId5 = string;
export type Status2 = "accepted" | "rejected" | "pending";
export type ReasonCode = string;
export type StateRevision2 = number;
export type SchemaVersion17 = "1.0";
export type RecordId = string;
export type AttemptId3 = string;
export type TaskId5 = string;
export type Inputs2 = Ref[];
export type Tools = {
  [k: string]: JsonValue;
}[];
export type Artifacts = string[];
export type Diff = string;
export type CheckOutputs = string[];
export type ElapsedMs = number;
export type CallCount = number;
export type RetryHistory = string[];
export type Mode4 = "mock" | "real_tool" | "real_model";

export interface ContractBundle {
  schema_version: SchemaVersion;
  requirement: Requirement;
  design: Design;
  plan: Plan;
  run: Run;
  attempt: Attempt;
  artifact: Artifact;
  review: Review;
  event: Event;
  command: Command;
  receipt: Receipt;
  execution_record: ExecutionRecord;
}
export interface Requirement {
  schema_version: SchemaVersion1;
  object_id: ObjectId;
  version: Version;
  content_hash: ContentHash;
  provenance: Provenance;
  content: RequirementContent;
  kind: Kind1;
  confirmation: Confirmation | null;
}
export interface Provenance1 {
  schema_version: SchemaVersion2;
  kind: Kind;
  source_id: SourceId;
  locator: Locator;
}
export interface RequirementContent {
  requirements: Requirements;
  out_of_scope: OutOfScope;
}
export interface RequirementItem {
  id: Id;
  text: Text;
  acceptance: Acceptance;
}
export interface Confirmation {
  schema_version: SchemaVersion3;
  confirmation_id: ConfirmationId;
  actor: Actor;
  origin: Origin;
  target: Ref;
  dependencies: Dependencies;
  confirmed_at: ConfirmedAt;
}
export interface Ref {
  schema_version: SchemaVersion4;
  object_id: ObjectId1;
  version: Version1;
  content_hash: ContentHash1;
}
export interface Design {
  schema_version: SchemaVersion5;
  object_id: ObjectId2;
  version: Version2;
  content_hash: ContentHash2;
  provenance: Provenance2;
  content: DesignContent;
  kind: Kind2;
  requirement: Ref;
}
export interface DesignContent {
  modules: Modules;
  data: Data;
  interfaces: Interfaces;
  states: States;
  concurrency: Concurrency;
  exceptions: Exceptions;
  interaction: Interaction;
  decisions: Decisions;
  elements: Elements;
}
export interface DesignElement {
  id: Id1;
  sources: Sources;
  description: Description;
  acceptance_method: AcceptanceMethod;
}
export interface Plan {
  schema_version: SchemaVersion6;
  object_id: ObjectId3;
  version: Version3;
  content_hash: ContentHash3;
  provenance: Provenance3;
  content: Content;
  kind: Kind3;
  requirement: Ref;
  design: Ref;
  tasks: Tasks;
}
export interface Content {
  [k: string]: JsonValue;
}
export interface Task {
  schema_version: SchemaVersion7;
  task_id: TaskId;
  version: Version4;
  content_hash: ContentHash4;
  type: Type;
  goal: Goal;
  sources: Sources1;
  inputs: Inputs;
  outputs: Outputs;
  depends_on: DependsOn;
  executor: Executor;
  reviewer: Reviewer;
  write_scope: WriteScope;
  checks: Checks;
  resources: Resources;
  priority: Priority;
  policy: AttemptPolicy;
  required: Required;
  api_contracts?: ApiContracts;
}
export interface AttemptPolicy {
  schema_version: SchemaVersion8;
  max_auto_retries: MaxAutoRetries;
  timeout_seconds: TimeoutSeconds;
}
export interface Run {
  schema_version: SchemaVersion9;
  run_id: RunId;
  mode: Mode;
  approved_package: ApprovedPackage;
  state_revision: StateRevision;
  event_seq: EventSeq;
  status: Status;
  call_budget: CallBudget;
  calls_used: CallsUsed;
  tasks: Tasks1;
}
export interface TaskRuntime {
  schema_version: SchemaVersion10;
  task_id: TaskId1;
  state: State;
  verification_status: VerificationStatus;
  disposition: Disposition;
  disposition_reason: DispositionReason;
  generation: Generation;
}
export interface Attempt {
  schema_version: SchemaVersion11;
  attempt_id: AttemptId;
  logical_task_id: LogicalTaskId;
  run_id: RunId1;
  task_id: TaskId2;
  generation: Generation1;
  input_hash: InputHash;
  provider_request_id: ProviderRequestId;
  idempotency_key: IdempotencyKey;
  mode: Mode1;
  status: Status1;
  auto_retry_count: AutoRetryCount;
  human_retry_count: HumanRetryCount;
}
export interface Artifact {
  schema_version: SchemaVersion12;
  artifact_id: ArtifactId;
  run_id: RunId2;
  task_id: TaskId3;
  attempt_id: AttemptId1;
  location: Location;
  content_hash: ContentHash5;
  inputs: Inputs1;
  implementation_version: ImplementationVersion;
  test_version: TestVersion;
  fixture_version: FixtureVersion;
  mode: Mode2;
}
export interface Review {
  schema_version: SchemaVersion13;
  review_id: ReviewId;
  target: Ref;
  author: Author;
  reviewer: Reviewer1;
  verdict: Verdict;
  findings: Findings;
  evidence: Evidence;
  mode: Mode3;
}
export interface Event {
  schema_version: SchemaVersion14;
  event_id: EventId;
  run_id: RunId3;
  event_seq: EventSeq1;
  task_id: TaskId4;
  attempt_id: AttemptId2;
  old_state: OldState;
  new_state: NewState;
  reason: Reason;
  actor_type: ActorType;
  occurred_at: OccurredAt;
  state_revision: StateRevision1;
  snapshot: Run;
}
export interface Command {
  schema_version: SchemaVersion15;
  operation_id: OperationId;
  run_id: RunId4;
  actor: Actor1;
  action: Action;
  target_id: TargetId;
  expected_version: ExpectedVersion;
  expected_state_revision: ExpectedStateRevision;
  parameters: Parameters;
}
export interface Parameters {
  [k: string]: JsonValue;
}
export interface Receipt {
  schema_version: SchemaVersion16;
  operation_id: OperationId1;
  run_id: RunId5;
  status: Status2;
  reason_code: ReasonCode;
  state_revision: StateRevision2;
}
export interface ExecutionRecord {
  schema_version: SchemaVersion17;
  record_id: RecordId;
  attempt_id: AttemptId3;
  task_id: TaskId5;
  inputs: Inputs2;
  tools: Tools;
  artifacts: Artifacts;
  diff: Diff;
  check_outputs: CheckOutputs;
  elapsed_ms: ElapsedMs;
  call_count: CallCount;
  retry_history: RetryHistory;
  mode: Mode4;
}
