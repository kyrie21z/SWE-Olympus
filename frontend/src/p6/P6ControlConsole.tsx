import {
  Alert,
  App as AntApp,
  Badge,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Statistic,
  Steps,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDot,
  Clock3,
  Command,
  Database,
  Gauge,
  GitBranch,
  ListTree,
  LockKeyhole,
  Pause,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  TerminalSquare,
  UserRoundCheck,
  WifiOff,
  Workflow,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Command as OlympusCommand } from "../contracts/generated";
import { mergeEvents, parseSnapshot } from "../data/core/events";
import type {
  CommandResult,
  ExecutionApiView,
  P6OlympusClient,
  RunView,
  TrailView,
} from "../data/core/run-view";
import { HttpApiError, HttpOlympusClient } from "../data/http/client";
import { P6Dag } from "./P6Dag";
import {
  P6_COMMANDS,
  allowedActionFor,
  buildLanes,
  buildTaskViewsFromView,
  commandAvailabilityFromView,
  commandTargets,
  createCommandForView,
  deriveStagesFromView,
  deriveTraceRowsFromView,
  formatRunStatus,
  formatTaskState,
  formatVerification,
  type P6CommandAction,
  type P6CoverageStatus,
  type P6StageStatus,
  type P6TaskView,
} from "./model";

const defaultClient = new HttpOlympusClient(
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "",
);

const stageStatusMap: Record<
  P6StageStatus,
  "wait" | "process" | "finish" | "error"
> = {
  waiting: "wait",
  in_progress: "process",
  waiting_human: "process",
  accepted: "finish",
  blocked: "error",
};

const stateColor: Record<string, string> = {
  waiting: "default",
  ready: "blue",
  running: "processing",
  verifying: "purple",
  accepted: "success",
  failed: "error",
  blocked: "error",
  cancelled: "default",
};

const coverageColor: Record<P6CoverageStatus, string> = {
  verified: "success",
  unverified: "error",
  risk_accepted: "warning",
  in_progress: "processing",
  pending: "default",
};

interface P6ControlConsoleProps {
  client?: P6OlympusClient;
  runId?: string;
}

function stateLabel(task: P6TaskView): string {
  if (task.runtime.disposition === "risk_accepted") {
    return "未验证 · 人工接受";
  }
  return formatTaskState(task.runtime.state);
}

function selectedFirstTask(tasks: P6TaskView[]): string {
  return (
    tasks.find(({ runtime }) =>
      ["blocked", "failed"].includes(runtime.state),
    )?.task.task_id ??
    tasks.find(({ runtime }) =>
      ["running", "verifying"].includes(runtime.state),
    )?.task.task_id ??
    tasks[0]?.task.task_id ??
    ""
  );
}

function apiErrorText(error: unknown): string {
  if (error instanceof HttpApiError) {
    return `${error.code} · ${error.message}`;
  }
  return error instanceof Error ? error.message : "请求失败";
}

function executionUnavailable(error: unknown): string {
  if (error instanceof HttpApiError) {
    return `${error.code} · ${error.message}`;
  }
  return error instanceof Error ? error.message : "执行详情不可用";
}

export function P6ControlConsole({
  client = defaultClient,
  runId: suppliedRunId,
}: P6ControlConsoleProps) {
  const { message } = AntApp.useApp();
  const runId =
    suppliedRunId ??
    new URLSearchParams(window.location.search).get("run") ??
    "RUN-campus";
  const [view, setView] = useState<RunView>();
  const [trail, setTrail] = useState<TrailView>();
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const snapshotRef = useRef<ReturnType<typeof parseSnapshot> | undefined>(
    undefined,
  );
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [execution, setExecution] = useState<ExecutionApiView>();
  const [executionError, setExecutionError] = useState("");
  const [executionLoading, setExecutionLoading] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandAction, setCommandAction] =
    useState<P6CommandAction>("resume");
  const [targetId, setTargetId] = useState("");
  const [reason, setReason] = useState("");
  const [prompt, setPrompt] = useState("");
  const [assignee, setAssignee] = useState("");
  const [changeLevel, setChangeLevel] = useState<
    "task" | "design" | "requirement"
  >("task");
  const [changeSummary, setChangeSummary] = useState("");
  const [receiptResult, setReceiptResult] = useState<CommandResult>();
  const [lastCommand, setLastCommand] = useState<OlympusCommand>();
  const [submitting, setSubmitting] = useState(false);

  const loadView = useCallback(async () => {
    try {
      const incoming = await client.getView(runId);
      const snapshot = parseSnapshot(incoming.snapshot);
      setView({ ...incoming, snapshot });
      snapshotRef.current = snapshot;
      setError("");
      setStale(false);
      try {
        setTrail(await client.getTrail(runId));
      } catch {
        setTrail(undefined);
      }
    } catch (requestError) {
      setError(apiErrorText(requestError));
      setStale(true);
    } finally {
      setLoading(false);
    }
  }, [client, runId]);

  const syncEvents = useCallback(async () => {
    const current = snapshotRef.current;
    if (!current) {
      await loadView();
      return;
    }
    try {
      const events = await client.getEvents(runId, current.event_seq);
      const merged = mergeEvents(current, events);
      if (merged.needsRefetch || merged.applied > 0) {
        await loadView();
      } else {
        setStale(false);
      }
    } catch {
      setStale(true);
    }
  }, [client, loadView, runId]);

  useEffect(() => {
    setLoading(true);
    void loadView();
  }, [loadView]);

  useEffect(() => {
    if (!view) return;
    let active = true;
    const timer = window.setInterval(() => {
      if (active) void syncEvents();
    }, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [syncEvents, view?.run_id]);

  const taskViews = useMemo(
    () => (view ? buildTaskViewsFromView(view) : []),
    [view],
  );
  const stages = useMemo(
    () => (view ? deriveStagesFromView(view, taskViews) : []),
    [taskViews, view],
  );
  const traceRows = useMemo(
    () => (view ? deriveTraceRowsFromView(view, taskViews) : []),
    [taskViews, view],
  );
  const lanes = useMemo(() => buildLanes(taskViews), [taskViews]);
  const selectedTask =
    taskViews.find(({ task }) => task.task_id === selectedTaskId) ??
    taskViews[0];

  useEffect(() => {
    if (
      taskViews.length &&
      !taskViews.some((task) => task.task.task_id === selectedTaskId)
    ) {
      setSelectedTaskId(selectedFirstTask(taskViews));
    }
  }, [selectedTaskId, taskViews]);

  useEffect(() => {
    if (!view || !selectedTaskId) return;
    let active = true;
    setExecutionLoading(true);
    setExecutionError("");
    setExecution(undefined);
    void client
      .getExecution(runId, selectedTaskId)
      .then((result) => {
        if (active) setExecution(result);
      })
      .catch((requestError) => {
        if (active) setExecutionError(executionUnavailable(requestError));
      })
      .finally(() => {
        if (active) setExecutionLoading(false);
      });
    return () => {
      active = false;
    };
  }, [client, runId, selectedTaskId, view]);

  if (loading && !view) {
    return (
      <Card className="p6-loading-card">
        <Spin /> 正在读取权威运行视图…
      </Card>
    );
  }

  if (!view) {
    return (
      <Card className="p6-error-card">
        <Empty
          description="无法读取运行视图"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
        <Alert
          type="error"
          showIcon
          title={error || "RUN_NOT_FOUND"}
          description="请确认 P5 后端已启动，且 OLYMPUS_DB 中存在该运行。"
        />
        <Button
          type="primary"
          icon={<RefreshCw size={15} />}
          onClick={() => {
            setLoading(true);
            void loadView();
          }}
        >
          重新连接
        </Button>
      </Card>
    );
  }

  const currentView: RunView = view;
  const run = view.snapshot;
  const requiredTasks = taskViews.filter(({ task }) => task.required);
  const accepted = view.projection.progress.accepted;
  const required = view.projection.progress.required;
  const blockedTasks = taskViews.filter(({ runtime }) =>
    ["blocked", "failed"].includes(runtime.state),
  );
  const activeTasks = taskViews.filter(({ runtime }) =>
    ["running", "verifying", "ready"].includes(runtime.state),
  );
  const completion = required
    ? Math.round((accepted / required) * 100)
    : 0;
  const currentStage = Math.max(
    0,
    stages.findIndex((stage) => stage.status !== "accepted"),
  );
  const commandDefinition =
    P6_COMMANDS.find((item) => item.action === commandAction) ??
    P6_COMMANDS[0];
  const targets = commandTargets(commandAction, run, taskViews);
  const effectiveTargetId =
    targets.find((target) => target.value === targetId)?.value ??
    targets[0]?.value ??
    "";
  const commandDisabled = commandAvailabilityFromView(view, commandAction);

  function openCommands(action?: P6CommandAction) {
    const firstEnabled = currentView.allowed_actions.find(
      (item) => item.enabled,
    );
    const preferred =
      action && allowedActionFor(currentView, action)?.enabled
        ? action
        : firstEnabled?.action ?? P6_COMMANDS[0].action;
    setCommandAction(preferred);
    setTargetId(commandTargets(preferred, run, taskViews)[0]?.value ?? "");
    setReason("");
    setPrompt("");
    setAssignee("");
    setChangeLevel("task");
    setChangeSummary("");
    setReceiptResult(undefined);
    setCommandOpen(true);
  }

  async function submitCommand(command?: OlympusCommand) {
    const nextCommand =
      command ??
      createCommandForView(commandAction, currentView, effectiveTargetId, {
        reason,
        prompt: prompt || reason,
        assignee,
        level: changeLevel,
        summary: changeSummary,
      });
    if (!effectiveTargetId && !command) return;
    if (commandAction === "accept_risk" && !reason.trim() && !command) {
      void message.warning("接受风险必须填写原因。");
      return;
    }
    if (commandAction === "reassign" && !assignee.trim() && !command) {
      void message.warning("改派必须填写目标执行者。");
      return;
    }
    if (
      commandAction === "request_change" &&
      (!changeSummary.trim() || !command)
    ) {
      void message.warning("变更请求必须填写摘要。");
      return;
    }

    setSubmitting(true);
    setReceiptResult(undefined);
    try {
      const result = await client.submitDetailed(nextCommand);
      setReceiptResult(result);
      setLastCommand(nextCommand);
      if (result.receipt.status === "accepted") {
        void message.success("命令已由服务端接纳。");
        await loadView();
      } else {
        void message.warning(`命令未接纳：${result.receipt.reason_code}`);
      }
    } catch (requestError) {
      void message.error(apiErrorText(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  const traceColumns = [
    {
      title: "需求",
      key: "requirement",
      width: 250,
      render: (_: unknown, row: (typeof traceRows)[number]) => (
        <div className="p6-req-cell">
          <strong>{row.req_id}</strong>
          <span>{row.req_text}</span>
        </div>
      ),
    },
    {
      title: "设计元素",
      dataIndex: "ds_ids",
      key: "design",
      width: 130,
      render: (value: string[]) =>
        value.map((id) => (
          <Tag key={id} className="p6-code-tag">
            {id}
          </Tag>
        )),
    },
    {
      title: "任务",
      dataIndex: "all_tasks",
      key: "tasks",
      width: 260,
      render: (value: string[], row: (typeof traceRows)[number]) => (
        <div className="p6-task-tags">
          {value.map((taskId) => {
            const task = taskViews.find(
              (taskView) => taskView.task.task_id === taskId,
            );
            return (
              <Button
                key={taskId}
                size="small"
                type="text"
                className="p6-task-chip"
                onClick={() => setSelectedTaskId(taskId)}
              >
                <span
                  className={`p6-chip-dot p6-state-${task?.runtime.state ?? "waiting"}`}
                />
                {taskId}
              </Button>
            );
          })}
          <span className="p6-table-note">
            {row.taskStates.filter((state) => state === "accepted").length}/
            {row.taskStates.length} accepted
          </span>
        </div>
      ),
    },
    {
      title: "覆盖结论",
      key: "coverage",
      width: 160,
      render: (_: unknown, row: (typeof traceRows)[number]) => (
        <Tag color={coverageColor[row.coverage]}>{row.coverageLabel}</Tag>
      ),
    },
  ];

  return (
    <div className="p6-console" data-testid="p6-console">
      <section className="p6-command-band">
        <div>
          <span className="eyebrow">
            <Workflow size={14} /> P6 · 单运行控制台
          </span>
          <h2>{run.run_id}</h2>
          <p>
            {run.mode} · revision {run.state_revision} · 事件 #{run.event_seq}
          </p>
        </div>
        <div className="p6-command-actions">
          <Tag
            color={stale ? "warning" : "success"}
            icon={stale ? <WifiOff size={13} /> : <CircleDot size={13} />}
          >
            {stale ? "数据可能过期" : "事件已同步"}
          </Tag>
          <Tag color={run.status === "completed" ? "success" : "processing"}>
            {formatRunStatus(run)}
          </Tag>
          <Tooltip title="重新读取快照、投影、allowed_actions 和追踪矩阵">
            <Button
              aria-label="刷新运行视图"
              icon={<RefreshCw size={15} />}
              onClick={() => void syncEvents()}
            />
          </Tooltip>
          <Button
            type="primary"
            icon={<Command size={16} />}
            onClick={() => openCommands()}
          >
            人工命令
          </Button>
        </div>
      </section>

      {error && (
        <Alert
          type="error"
          showIcon
          title="运行视图刷新失败"
          description={error}
          action={
            <Button size="small" onClick={() => void loadView()}>
              重试
            </Button>
          }
        />
      )}

      {blockedTasks.length > 0 && (
        <Alert
          className="p6-block-alert"
          type="error"
          showIcon
          icon={<AlertTriangle size={18} />}
          title={`${blockedTasks
            .map(({ task }) => task.task_id)
            .join("、")} 阻塞，后继任务保持等待`}
          description={
            view.projection.sets.blocked
              .map((item) => `${item.task_id}: ${item.why}`)
              .join("；") || "等待后端允许的人工命令。"
          }
          action={
            <Button
              size="small"
              onClick={() =>
                openCommands(
                  allowedActionFor(view, "retry")?.enabled
                    ? "retry"
                    : undefined,
                )
              }
              icon={<RefreshCw size={14} />}
            >
              处理
            </Button>
          }
        />
      )}

      <section className="p6-stat-grid" aria-label="运行指标">
        <Card>
          <Statistic
            title="必要任务验收"
            value={accepted}
            suffix={`/ ${required}`}
            prefix={<CheckCircle2 size={18} />}
          />
          <Progress percent={completion} showInfo={false} size="small" />
        </Card>
        <Card>
          <Statistic
            title="服务端摘要 In-flight"
            value={view.projection.resources.inflight}
            prefix={<Activity size={18} />}
          />
          <span className="p6-stat-foot">
            ready {view.projection.sets.ready.length} · active{" "}
            {activeTasks.length}
          </span>
        </Card>
        <Card>
          <Statistic
            title="阻塞"
            value={view.projection.sets.blocked.length}
            prefix={<AlertTriangle size={18} />}
            styles={
              view.projection.sets.blocked.length
                ? { content: { color: "#d4380d" } }
                : undefined
            }
          />
          <span className="p6-stat-foot">
            {view.projection.sets.blocked.map((item) => item.task_id).join("、") ||
              "无"}
          </span>
        </Card>
        <Card>
          <Statistic
            title="调用预算"
            value={run.calls_used}
            suffix={`/ ${run.call_budget}`}
            prefix={<Gauge size={18} />}
          />
          <Progress
            percent={
              run.call_budget
                ? Math.round((run.calls_used / run.call_budget) * 100)
                : 0
            }
            showInfo={false}
            size="small"
          />
        </Card>
      </section>

      <Card
        className="p6-stage-card"
        title={
          <span className="p6-card-title">
            <ListTree size={17} />
            阶段进度
          </span>
        }
        extra={
          <Tag icon={<Clock3 size={13} />}>
            {stages[currentStage]?.title ?? "已完成"}
          </Tag>
        }
      >
        <Steps
          current={currentStage}
          items={stages.map((stage) => ({
            title: stage.title,
            content: stage.description,
            status: stageStatusMap[stage.status],
          }))}
        />
      </Card>

      <div className="p6-main-grid">
        <Card
          className="p6-dag-card"
          title={
            <span className="p6-card-title">
              <GitBranch size={17} />
              只读业务 DAG
            </span>
          }
          extra={
            <Space size={8}>
              <Tag>{view.projection.manifest.manifest_id}</Tag>
              <Tag color="blue">
                {view.projection.manifest.critical_chain.join(" → ")}
              </Tag>
            </Space>
          }
        >
          <P6Dag
            view={view}
            tasks={taskViews}
            selectedTaskId={selectedTask?.task.task_id ?? ""}
            onSelect={setSelectedTaskId}
          />
          <div className="p6-legend">
            <span>
              <i className="p6-dot p6-state-accepted" />
              已验收
            </span>
            <span>
              <i className="p6-dot p6-state-running" />
              执行中
            </span>
            <span>
              <i className="p6-dot p6-state-blocked" />
              阻塞
            </span>
            <span>
              <i className="p6-dot p6-state-waiting" />
              等待
            </span>
            <span>
              <i className="p6-critical-line" />
              服务端关键链
            </span>
          </div>
        </Card>

        <Card
          className="p6-detail-card"
          title={
            <span className="p6-card-title">
              <Database size={17} />
              任务与执行证据
            </span>
          }
          extra={<Tag icon={<LockKeyhole size={12} />}>只读</Tag>}
        >
          {selectedTask ? (
            <>
              <div className="p6-detail-heading">
                <div>
                  <span>{selectedTask.task.task_id}</span>
                  <h3>{selectedTask.task.goal}</h3>
                </div>
                <Tag color={stateColor[selectedTask.runtime.state]}>
                  {stateLabel(selectedTask)}
                </Tag>
              </div>
              <Descriptions
                column={1}
                size="small"
                items={[
                  {
                    key: "agent",
                    label: "执行 / 复核",
                    children: `${selectedTask.task.executor} / ${selectedTask.task.reviewer}`,
                  },
                  {
                    key: "verification",
                    label: "验证状态",
                    children: formatVerification(
                      selectedTask.runtime.verification_status,
                    ),
                  },
                  {
                    key: "missing",
                    label: "缺失前驱",
                    children:
                      selectedTask.projection?.missing_predecessors.join("、") ||
                      "无",
                  },
                  {
                    key: "sources",
                    label: "需求 / 设计来源",
                    children: selectedTask.task.sources.join("、"),
                  },
                  {
                    key: "generation",
                    label: "执行代际",
                    children: `generation ${selectedTask.runtime.generation}`,
                  },
                ]}
              />

              {executionLoading && <Spin size="small" />}
              {executionError && (
                <Alert
                  className="p6-execution-alert"
                  type="warning"
                  showIcon
                  title="执行详情不可用"
                  description={executionError}
                />
              )}
              {execution && (
                <Collapse
                  className="p6-execution-collapse"
                  defaultActiveKey={["execution"]}
                  items={[
                    {
                      key: "execution",
                      label: `执行 ${execution.execution.attempt_id ?? selectedTask.task.task_id}`,
                      children: (
                        <div className="p6-execution-detail">
                          <div className="p6-execution-tags">
                            <Tag>{execution.execution.state ?? "unknown"}</Tag>
                            <Tag>{execution.execution.model_mode ?? "mock"}</Tag>
                            <Tag>{execution.execution.tool_mode ?? "unknown"}</Tag>
                            {typeof execution.execution.tool_invocations ===
                              "number" && (
                              <Tag>
                                tools {execution.execution.tool_invocations}
                              </Tag>
                            )}
                          </div>
                          <pre className="p6-diff">
                            {execution.execution.diff || "无 diff"}
                          </pre>
                          <strong>检查输出</strong>
                          <pre className="p6-checks">
                            {JSON.stringify(
                              execution.execution.check_outputs ?? [],
                              null,
                              2,
                            )}
                          </pre>
                          <strong>工件</strong>
                          <pre className="p6-checks">
                            {JSON.stringify(
                              execution.execution.artifacts ?? [],
                              null,
                              2,
                            )}
                          </pre>
                          <strong>复核请求</strong>
                          <pre className="p6-checks">
                            {JSON.stringify(
                              execution.review_requests ?? [],
                              null,
                              2,
                            )}
                          </pre>
                        </div>
                      ),
                    },
                  ]}
                />
              )}
            </>
          ) : (
            <Empty description="选择一个任务" />
          )}
        </Card>
      </div>

      <div className="p6-lower-grid">
        <Card
          className="p6-lane-card"
          title={
            <span className="p6-card-title">
              <Bot size={17} />
              Agent 泳道
            </span>
          }
          extra={<Tag>服务端优先级</Tag>}
        >
          <div className="p6-lane-table">
            {lanes.map((lane) => (
              <div className="p6-lane-row" key={lane.agent}>
                <div className="p6-lane-agent">
                  <span>{lane.agent.slice(0, 1)}</span>
                  <div>
                    <strong>{lane.agent}</strong>
                    <small>{lane.tasks.length} 个任务</small>
                  </div>
                </div>
                <div className="p6-lane-track">
                  {lane.tasks.map(({ task, runtime }) => (
                    <button
                      key={task.task_id}
                      type="button"
                      className={[
                        "p6-lane-block",
                        `p6-state-${runtime.state}`,
                        task.task_id === selectedTask?.task.task_id
                          ? "p6-selected"
                          : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      data-p6-lane-task={task.task_id}
                      onClick={() => setSelectedTaskId(task.task_id)}
                    >
                      <span>{task.task_id}</span>
                      <small>{stateLabel({ task, runtime })}</small>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {trail?.entries.length ? (
            <div className="p6-trail">
              <strong>命令痕迹</strong>
              {trail.entries.slice(-4).map((entry, index) => (
                <span key={index}>
                  {String(entry.action ?? "command")} ·{" "}
                  {String(entry.task_id ?? entry.target_id ?? "")}
                </span>
              ))}
            </div>
          ) : (
            <p className="p6-card-foot">
              命令痕迹与执行详情直接来自 P5/P4，不在前端重拼。
            </p>
          )}
        </Card>

        <Card
          className="p6-trace-card"
          title={
            <span className="p6-card-title">
              <ShieldCheck size={17} />
              需求追踪表
            </span>
          }
          extra={
            <Badge
              status={view.trace ? "processing" : "error"}
              text={
                view.trace
                  ? `${traceRows.filter((row) => row.coverage === "verified").length} / ${traceRows.length} 已验证`
                  : "追踪不可用"
              }
            />
          }
        >
          {view.trace ? (
            <Table
              rowKey="req_id"
              size="small"
              columns={traceColumns}
              dataSource={traceRows}
              pagination={false}
              scroll={{ x: 780 }}
            />
          ) : (
            <Empty description="服务端未提供追踪矩阵" />
          )}
        </Card>
      </div>

      <Modal
        open={commandOpen}
        title={
          <span className="p6-modal-title">
            <TerminalSquare size={19} />
            统一人工命令
          </span>
        }
        onCancel={() => setCommandOpen(false)}
        footer={null}
        width={640}
      >
        <div className="p6-command-form">
          <div className="p6-command-context">
            <span>{run.run_id}</span>
            <span>schema v{run.schema_version}</span>
            <span>revision {run.state_revision}</span>
            <span>event #{run.event_seq}</span>
          </div>

          <label className="p6-field">
            <span>命令</span>
            <Select
              aria-label="命令类型"
              value={commandAction}
              onChange={(value) => {
                setCommandAction(value);
                setReceiptResult(undefined);
                setTargetId(
                  commandTargets(value, run, taskViews)[0]?.value ?? "",
                );
              }}
              options={P6_COMMANDS.map((item) => {
                const allowed = allowedActionFor(view, item.action);
                return {
                  value: item.action,
                  label: item.label,
                  disabled: !allowed?.enabled,
                };
              })}
            />
            <small>
              {commandDefinition.description}
              {commandDisabled ? `；${commandDisabled}` : ""}
            </small>
          </label>

          <label className="p6-field">
            <span>目标</span>
            <Select
              aria-label="命令目标"
              value={effectiveTargetId}
              onChange={setTargetId}
              options={targets}
              placeholder="当前没有可用目标"
            />
          </label>

          {commandAction === "retry" && (
            <label className="p6-field">
              <span>重试提示</span>
              <Input.TextArea
                aria-label="重试提示"
                rows={2}
                value={prompt}
                placeholder="保留原失败记录，说明新提示或处理方式"
                onChange={(event) => setPrompt(event.target.value)}
              />
            </label>
          )}

          {commandAction === "accept_risk" && (
            <label className="p6-field">
              <span>
                风险理由<em>必填</em>
              </span>
              <Input.TextArea
                aria-label="风险理由"
                rows={3}
                value={reason}
                placeholder="风险接受后仍显示未验证，不解锁后继"
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          )}

          {commandAction === "reassign" && (
            <label className="p6-field">
              <span>
                目标执行者<em>必填</em>
              </span>
              <Input
                aria-label="目标执行者"
                value={assignee}
                placeholder="例如 Hephaestus"
                onChange={(event) => setAssignee(event.target.value)}
              />
            </label>
          )}

          {commandAction === "request_change" && (
            <>
              <label className="p6-field">
                <span>回退层级</span>
                <Select
                  aria-label="回退层级"
                  value={changeLevel}
                  onChange={setChangeLevel}
                  options={[
                    { value: "task", label: "任务" },
                    { value: "design", label: "设计" },
                    { value: "requirement", label: "需求" },
                  ]}
                />
              </label>
              <label className="p6-field">
                <span>
                  变更摘要<em>必填</em>
                </span>
                <Input.TextArea
                  aria-label="变更摘要"
                  rows={3}
                  value={changeSummary}
                  onChange={(event) => setChangeSummary(event.target.value)}
                />
              </label>
            </>
          )}

          {commandDisabled && (
            <Alert
              type="warning"
              showIcon
              title={commandDisabled}
              icon={<ShieldAlert size={16} />}
            />
          )}

          {receiptResult && (
            <Alert
              data-testid="p6-command-receipt"
              type={
                receiptResult.receipt.status === "accepted"
                  ? "success"
                  : "error"
              }
              showIcon
              title={`${receiptResult.receipt.status} · ${receiptResult.receipt.reason_code}`}
              description={`operation ${receiptResult.receipt.operation_id} · revision ${receiptResult.receipt.state_revision} · durability ${receiptResult.durability} · recorded ${receiptResult.recordedEvents}`}
            />
          )}

          <div className="p6-command-note">
            <ShieldCheck size={15} />
            可用动作、禁用原因和版本校验均来自后端。命令成功后重新读取权威视图。
          </div>

          <div className="p6-command-footer">
            {lastCommand && (
              <Button
                onClick={() => void submitCommand(lastCommand)}
                disabled={submitting}
              >
                再次提交同一 operation
              </Button>
            )}
            <span className="p6-footer-spacer" />
            <Button onClick={() => setCommandOpen(false)}>取消</Button>
            <Button
              type="primary"
              loading={submitting}
              disabled={Boolean(commandDisabled) || !effectiveTargetId}
              icon={
                commandAction === "pause" ? (
                  <Pause size={15} />
                ) : commandAction === "resume" ? (
                  <Play size={15} />
                ) : commandAction === "reassign" ? (
                  <UserRoundCheck size={15} />
                ) : (
                  <Command size={15} />
                )
              }
              onClick={() => void submitCommand()}
            >
              提交命令
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
