import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Collapse, Descriptions, Empty, Radio, Space, Spin, Steps, Table, Tabs, Tag } from "antd";
import { Download, RefreshCw, Settings2, Workflow } from "lucide-react";
import type { Requirement } from "../contracts/generated";
import { InspectionPanel, RequirementDetails } from "../imports/Preview";
import { downloadText, type ImportedRequirement, type Inspection } from "../imports/model";
import { describeApiError, WorkflowApiError, workflowClient, type WorkflowAction, type WorkflowModelPreference, type WorkflowSnapshot } from "./client";
import { BUSY_STAGES, NEXT_ACTION, readableJson, STAGES, workflowStageIndex } from "./model";
import "./workflow.css";

const LINK_KEY = "olympus.workflow-links.v1";
function rememberedWorkflow(importId: string): string {
  try {const data: unknown = JSON.parse(localStorage.getItem(LINK_KEY) ?? "{}"); return data && typeof data === "object" && typeof (data as Record<string, unknown>)[importId] === "string" ? (data as Record<string, string>)[importId] : "";} catch {return "";}
}
function rememberWorkflow(importId: string, id: string) {
  let data: Record<string, string> = {};
  try {const raw: unknown = JSON.parse(localStorage.getItem(LINK_KEY) ?? "{}"); if (raw && typeof raw === "object" && !Array.isArray(raw)) data = raw as Record<string, string>;} catch { /* A valid new association can replace corrupt metadata. */ }
  localStorage.setItem(LINK_KEY, JSON.stringify({...data, [importId]: id}));
}
function jsonObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function Pending({title, description}: {title: string; description: string}) {
  return <div className="workflow-pending"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false} /><h3>{title}</h3><p>{description}</p></div>;
}

function reviewFindings(value: unknown) {
  const outer = jsonObject(value);
  const submission = jsonObject(outer.submission);
  const review = jsonObject(outer.review ?? value);
  if (Array.isArray(submission.findings)) {
    return submission.findings.map(item => {
      const finding = jsonObject(item);
      return {
        message: typeof finding.message === "string" ? finding.message : "审查意见",
        id: typeof finding.finding_id === "string" ? finding.finding_id : null,
        severity: finding.severity,
        closed: finding.status === "closed",
        closure: typeof finding.closure_reason === "string" ? finding.closure_reason : "",
      };
    });
  }
  return (Array.isArray(review.findings) ? review.findings : []).map(item => ({
    id: null, message: typeof item === "string" ? item : readableJson(item), severity: undefined, closed: false, closure: "",
  }));
}

function Review({value}: {value: unknown}) {
  if (!value) return null;
  const outer = jsonObject(value);
  const review = jsonObject(outer.review ?? value);
  const verdict = typeof review.verdict === "string" ? review.verdict : "已返回报告";
  const findings = reviewFindings(value);
  const label = {passed: "通过", blocked: "有阻断项", needs_revision: "待修订"}[verdict as "passed" | "blocked" | "needs_revision"] ?? verdict;
  return <div className="workflow-review"><Space wrap><strong>独立审查</strong><Tag color={verdict === "passed" ? "success" : "warning"}>{label}</Tag></Space>
    {findings.length > 0 && <ul>{findings.map((finding, i) => <li key={finding.id ?? i}>
      {finding.closed ? <Tag color="success">已核对</Tag> : finding.severity === "blocker" ? <Tag color="error">阻断</Tag> : null}
      {finding.message}
      {finding.closure && <p>{finding.closure}</p>}
    </li>)}</ul>}
    <Collapse items={[{key: "review", label: "查看完整独立审查报告", children: <pre className="workflow-json">{readableJson(value)}</pre>}]} />
  </div>;
}
function ExecutionEvidence({value}: {value: unknown}) {
  const row = jsonObject(value);
  const execution = jsonObject(row.execution ?? row.receipt ?? value);
  const runtime = jsonObject(row.runtime);
  const checks = Array.isArray(execution.check_outputs) ? execution.check_outputs : [];
  const artifacts = Array.isArray(execution.artifacts) ? execution.artifacts : [];
  return <div className="workflow-artifact">
    <Descriptions size="small" column={{xs:1,sm:2}} items={[
      {key:"state",label:"任务状态",children:String(runtime.state ?? execution.state ?? "未提供")},
      {key:"tool",label:"工具模式",children:String(execution.tool_mode ?? execution.mode ?? "未提供")},
      {key:"model",label:"模型模式",children:String(execution.model_mode ?? "未提供")},
      {key:"attempt",label:"执行尝试",children:<span className="workflow-key">{String(row.attempt_id ?? execution.attempt_id ?? "未提供")}</span>},
    ]} />
    {artifacts.length > 0 && <><h4>实际工件</h4><Table size="small" pagination={false} scroll={{x:500}} rowKey={(_, index) => String(index)} dataSource={artifacts.map(jsonObject)} columns={[{title:"工件",render:(_, artifact) => String(artifact.location ?? artifact.path ?? artifact.artifact_id ?? "工件")},{title:"内容指纹",dataIndex:"content_hash",render:(hash:unknown) => <code className="workflow-key">{typeof hash === "string" ? hash : "未提供"}</code>}]} /></>}
    {typeof execution.diff === "string" && execution.diff && <><h4>实现差异</h4><pre className="workflow-json">{execution.diff}</pre></>}
    {checks.length > 0 && <><h4>实际检查输出</h4><Collapse items={checks.map((value, index) => {
      const check = jsonObject(value);
      return {key:String(index),label:`${String(check.check_id ?? index + 1)} · 退出码 ${String(check.exit_code ?? "未提供")}`,children:<pre className="workflow-json">{String(check.output ?? check.stdout ?? "无输出")}</pre>};
    })} /></>}
    <Review value={execution.review} />
    <Collapse items={[{key:"execution",label:"查看完整执行回执和证据",children:<pre className="workflow-json">{readableJson(value)}</pre>}]} />
  </div>;
}
function FrozenChecks({acceptance}: {acceptance: WorkflowSnapshot["acceptance"]}) {
  const entries = Object.entries(acceptance ?? {});
  if (!entries.length) return <Alert type="warning" showIcon title="尚未取得冻结验收检查，不能据此确认执行准备完成。" />;
  return <div className="workflow-artifact"><h3>冻结验收检查</h3><p className="workflow-intro">请在批准计划前审阅这些脚本。实现 Agent 不能修改验收定义；执行将运行这里冻结的实际检查。</p>
    <Collapse items={entries.map(([taskId, definition]) => {
      const frozen = jsonObject(definition);
      const checks = Object.entries(jsonObject(frozen.checks));
      return {key: taskId, label: `${taskId} · ${checks.length} 项检查`, children: <div className="workflow-artifact">
        {typeof frozen.content_hash === "string" && <span className="workflow-key">指纹：{frozen.content_hash}</span>}
        {checks.map(([checkId, script]) => <div key={checkId}><h4>{checkId}</h4><pre className="workflow-json workflow-script">{typeof script === "string" ? script : readableJson(script)}</pre></div>)}
        <Collapse items={[{key: "definition", label: "查看完整验收定义与夹具", children: <pre className="workflow-json">{readableJson(definition)}</pre>}]} />
      </div>};
    })} />
  </div>;
}

export function WorkflowWorkspace({file, report, onNewImport, onSettings}: {file: ImportedRequirement; report?: Inspection; onNewImport: () => void; onSettings: () => void}) {
  const [workflowId, setWorkflowId] = useState(() => rememberedWorkflow(file.id));
  const [snapshot, setSnapshot] = useState<WorkflowSnapshot | null>(null);
  const [tab, setTab] = useState("requirement");
  const [allowSynthetic, setAllowSynthetic] = useState(false);
  const [modelMode, setModelMode] = useState<"offline" | "online">();
  const [planRead, setPlanRead] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [missingWorkflow, setMissingWorkflow] = useState(false);
  const [storageError, setStorageError] = useState("");
  const mounted = useRef(true);
  const epoch = useRef(0);
  const pendingOperation = useRef<{action: WorkflowAction; id: string} | null>(null);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {setPlanRead(false);}, [snapshot?.plan?.content_hash]);
  useEffect(() => {
    if (!workflowId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const started = epoch.current;
      try {
        const next = await workflowClient.get(workflowId, controller.signal);
        if (!controller.signal.aborted && started === epoch.current) {
          setSnapshot(next); setLoadError(""); setMissingWorkflow(false);
          if (next.retry_safe) pendingOperation.current = null;
        }
      } catch (error) {
        if (!controller.signal.aborted && started === epoch.current) {
          setLoadError(describeApiError(error));
          setMissingWorkflow(error instanceof WorkflowApiError && error.code === "WORKFLOW_NOT_FOUND");
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 2000);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [workflowId]);
  async function refresh() {
    if (!workflowId || busy) return;
    setBusy(true);
    try {
      const next = await workflowClient.get(workflowId);
      if (!mounted.current) return;
      epoch.current++;
      setSnapshot(next); setLoadError(""); setMissingWorkflow(false);
    } catch (error) {
      if (!mounted.current) return;
      setLoadError(describeApiError(error));
      setMissingWorkflow(error instanceof WorkflowApiError && error.code === "WORKFLOW_NOT_FOUND");
    }
    finally {if (mounted.current) setBusy(false);}
  }
  async function create(mode: WorkflowModelPreference | undefined = modelMode) {
    if (!mode || !report?.requirement || !report.canSave || busy) return;
    const syntheticAllowed = allowSynthetic || snapshot?.allow_synthetic === true;
    if (report.status === "synthetic" && !syntheticAllowed) return;
    setBusy(true); setActionError(""); epoch.current++;
    try {
      const requirement = JSON.parse(file.sourceText.replace(/^\uFEFF/, "")) as Requirement;
      const next = await workflowClient.create(requirement, syntheticAllowed, mode);
      if (!mounted.current) return;
      epoch.current++;
      pendingOperation.current = null;
      setSnapshot(next); setWorkflowId(next.workflow_id);
      setTab("requirement"); setLoadError(""); setMissingWorkflow(false);
      try {rememberWorkflow(file.id, next.workflow_id);} catch {setStorageError("运行已建立，但浏览器未能保存关联。请保留运行编号，避免重复建立。");}
    } catch (error) {if (mounted.current) setActionError(describeApiError(error));}
    finally {if (mounted.current) setBusy(false);}
  }
  async function dispatch(action: WorkflowAction, nextTab?: string) {
    if (!snapshot || busy) return;
    setBusy(true); setActionError(""); epoch.current++;
    if (nextTab) setTab(nextTab);
    const operation = pendingOperation.current?.action === action ? pendingOperation.current : {action, id: crypto.randomUUID()};
    pendingOperation.current = operation;
    try {
      const next = await workflowClient.action(snapshot.workflow_id, action, operation.id);
      if (!mounted.current) return;
      epoch.current++;
      setSnapshot(next); pendingOperation.current = null;
    } catch (error) {if (mounted.current) setActionError(describeApiError(error));}
    finally {if (mounted.current) setBusy(false);}
  }
  const synthetic = report?.status === "synthetic";
  const generating = snapshot ? BUSY_STAGES.has(snapshot.stage) : false;
  const designRevision = snapshot?.stage === "design_generating" ? snapshot.design_revision : undefined;
  const stageStarted = designRevision?.started_at ?? snapshot?.updated_at;
  const elapsed = generating && stageStarted ? Math.floor((Date.now() - Date.parse(stageStarted)) / 1000) : 0;
  const next = snapshot ? NEXT_ACTION[snapshot.stage] : undefined;
  const permitted = !snapshot?.allowed_actions || (next && snapshot.allowed_actions.includes(next.action));
  const retry = snapshot?.retry_safe && snapshot.retry_action && snapshot.allowed_actions?.includes(snapshot.retry_action) ? snapshot.retry_action : null;
  const executionRows = snapshot ? Array.isArray(snapshot.executions) ? snapshot.executions : Object.values(snapshot.executions ?? {}) : [];
  const planBound = snapshot?.plan_bound ?? Boolean(snapshot?.plan && ["plan_approved", "executing", "execution_blocked", "awaiting_acceptance", "completed"].includes(snapshot.stage));
  const consoleUrl = snapshot && planBound ? `?run=${encodeURIComponent(snapshot.run_id)}#console` : null;
  const canCreate = !workflowId || missingWorkflow;
  const failedOnline = snapshot?.stage === "error" && snapshot.model_mode === "online" && !planBound;
  const reviewTab = snapshot?.stage === "design_review_blocked" ? "design" : snapshot?.stage === "plan_review_blocked" ? "plan" : null;
  const openFindings = reviewFindings(reviewTab === "design" ? snapshot?.design_review : snapshot?.plan_review)
    .filter(finding => !finding.closed);
  const canStartOffline = (failedOnline || Boolean(reviewTab)) && snapshot?.model_mode === "online" && snapshot.offline_supported;
  const planNeedsRead = next?.action === "approve_plan" && !planRead;
  const requirementContent = <>
    <div className="section-toolbar"><div><h3>需求交接内容</h3><p>原文件只读，补充说明独立保存。</p></div><Space wrap><Button icon={<Download size={15} />} onClick={() => downloadText(file.fileName, file.sourceText)}>导出原文件</Button><Button onClick={onNewImport}>导入新版本</Button></Space></div>
    {report ? <><InspectionPanel report={report} /><RequirementDetails file={file} report={report} /><Collapse items={[{key: "raw", label: "查看原始文件", children: <pre className="raw-source">{file.sourceText}</pre>}]} /></> : <Spin />}
  </>;
  const designContent = snapshot?.design ? <div className="workflow-artifact">
    <Descriptions size="small" column={1} items={[{key:"id",label:"设计",children:`${snapshot.design.object_id} · v${snapshot.design.version}`},{key:"hash",label:"内容指纹",children:<code className="workflow-key">{snapshot.design.content_hash}</code>},{key:"concurrency",label:"并发约定",children:snapshot.design.content.concurrency},{key:"interaction",label:"交互",children:snapshot.design.content.interaction}]} />
    <Space wrap>{snapshot.design.content.modules.map(value => <Tag key={value}>{value}</Tag>)}</Space>
    <Table rowKey="id" pagination={false} size="small" scroll={{x:700}} dataSource={snapshot.design.content.elements} columns={[{title:"设计元素",dataIndex:"id"},{title:"需求来源",dataIndex:"sources",render:(value:string[])=>value.join("、")},{title:"设计说明",dataIndex:"description"},{title:"验收方式",dataIndex:"acceptance_method"}]} />
    <Review value={snapshot.design_review} /><Collapse items={[{key:"design",label:"查看完整设计正文",children:<pre className="workflow-json">{readableJson(snapshot.design)}</pre>},
      ...(snapshot.design_review_history && snapshot.design_review_history.length > 1 ? [{
        key:"review-history", label:"查看设计审查历史", children:<Collapse items={snapshot.design_review_history!.map((value, i) => {
          const review = jsonObject(jsonObject(value).review);
          const version = jsonObject(review.target).version;
          return {key:String(i), label:`设计 v${String(version ?? "?")} · 第 ${i + 1} 份报告`, children:<Review value={value} />};
        })} />,
      }] : [])]} />
  </div> : <Pending title="设计规格尚未生成" description="接纳需求后即可生成设计。未配置模型 API 时，工作台使用匹配的离线样例。" />;
  const planContent = snapshot?.plan ? <div className="workflow-artifact">
    <Descriptions size="small" column={1} items={[{key:"id",label:"计划",children:`${snapshot.plan.object_id} · v${snapshot.plan.version}`},{key:"hash",label:"内容指纹",children:<code className="workflow-key">{snapshot.plan.content_hash}</code>}]} />
    <Table rowKey="task_id" pagination={false} size="small" scroll={{x:850}} dataSource={snapshot.plan.tasks} columns={[{title:"任务",dataIndex:"task_id",width:80},{title:"目标",dataIndex:"goal"},{title:"依赖",dataIndex:"depends_on",render:(value:string[])=>value.join("、")||"无"},{title:"执行者",dataIndex:"executor"},{title:"复核者",dataIndex:"reviewer"},{title:"检查",dataIndex:"checks",render:(value:string[])=>value.join("、")}]} />
    <Review value={snapshot.plan_review} /><FrozenChecks acceptance={snapshot.acceptance} />
    <Collapse items={[{key:"plan",label:"查看完整任务计划",children:<pre className="workflow-json">{readableJson(snapshot.plan)}</pre>}]} />
  </div> : <Pending title="任务计划尚未生成" description="批准当前设计后，由 Daedalus 规划任务，Momus 审查并生成独立验收检查。" />;
  const executionContent = <div className="workflow-artifact">
    {!consoleUrl && <Pending title="尚未批准任务计划" description="在工作台完成设计和任务规划，批准计划后即可打开运行控制台。" />}
    {consoleUrl && <Space wrap><Button type="primary" href={consoleUrl}>打开运行控制台</Button><Tag>{snapshot?.run_id}</Tag></Space>}
    {executionRows.length ? <Collapse items={executionRows.map((execution, i) => {const row = jsonObject(execution); return {key:String(i),label:`任务执行证据 · ${String(row.task_id ?? jsonObject(row.execution).task_id ?? i + 1)}`,children:<ExecutionEvidence value={execution} />};})} /> : consoleUrl && <Empty description="当前还没有实际执行证据" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
  </div>;
  const acceptanceContent = snapshot?.stage === "completed" ? <Alert type="success" showIcon title="当前运行已完成人工最终验收" description={`运行编号：${snapshot.run_id}。可在执行页和运行控制台查看证据。`} /> : <><Pending title={snapshot?.stage === "awaiting_acceptance" ? "全部必要任务已接纳，等待最终验收" : "暂无验收结果"} description="阅读实际检查、独立复核及任务执行证据后，再通过上方按钮确认最终验收。" />{snapshot && <Button onClick={() => setTab("execution")}>查看任务执行证据</Button>}</>;
  return <section className="workflow-workspace" data-testid="workflow-workspace">
    <Card title={<Space><Workflow size={18} />需求到执行</Space>} extra={<Button icon={<Settings2 size={15} />} onClick={onSettings}>Agent API 设置</Button>}>
      <div className="workflow-status-heading"><div><h3 data-testid="workflow-stage">{snapshot ? STAGES[snapshot.stage] ?? snapshot.stage : workflowId ? "正在恢复运行" : "建立当前需求的运行"}</h3><p>{snapshot ? <><span className="workflow-key">{snapshot.workflow_id}</span> · {snapshot.run_id}</> : "需求、设计、计划和最终验收均由你明确确认。"}</p></div>{generating && <Spin size="small" />}</div>
      <Steps className="workflow-steps" size="small" current={snapshot ? workflowStageIndex(snapshot) : 0} status={snapshot?.stage === "error" || snapshot?.stage.endsWith("_blocked") ? "error" : snapshot?.stage === "completed" ? "finish" : "process"} items={[{title:"需求接纳"},{title:"设计与审查"},{title:"计划与检查"},{title:"受控执行"},{title:"最终验收"}]} />
      {synthetic && <Alert type="info" showIcon title="构造测试需求" description="本次运行继续保留构造来源标识；测试确认不代表真实用户的生产授权。" />}
      {snapshot?.model_mode === "offline" && <Alert type="info" showIcon title="离线模式" description="使用与需求版本匹配的预置 Agent 响应。Python 检查在本机实际执行。" />}
      {actionError && <Alert type="error" showIcon title={actionError} />}
      {loadError && <Alert type="warning" showIcon title={missingWorkflow ? "找不到先前的运行，可为此文件重新建立运行" : "运行数据可能过期"} description={missingWorkflow ? undefined : loadError} />}
      {storageError && <Alert type="warning" showIcon title={storageError} />}
      {snapshot?.error && <Alert type="error" showIcon title={snapshot.error.code === "OUTPUTSCHEMAERROR" ? "模型已返回，设计或任务规格未通过格式检查。请重试当前阶段。" : `${snapshot.error.code}：${snapshot.error.message}`}
        action={["AGENT_NOT_CONFIGURED", "OUTCOME_UNKNOWN"].includes(snapshot.error.code) ? <Button onClick={onSettings}>检查 API 设置</Button> : undefined} />}
      {reviewTab && <Alert type="warning" showIcon title={openFindings.length ? `还有 ${openFindings.length} 条审查意见待处理` : "请查看当前审查报告"}
        description={openFindings.length ? <>
          {reviewTab === "design" && snapshot?.design_revision && <div>本次自动修订已完成 {snapshot.design_revision.round} 轮。下列意见仍待处理。</div>}
          <ul>{openFindings.map((finding, i) => <li key={finding.id ?? i}>{finding.message}</li>)}</ul>
        </> : undefined}
        action={<Button onClick={() => setTab(reviewTab)}>查看审查报告</Button>} />}
      {canCreate && report?.requirement && <div className="workflow-actions">
        <Radio.Group aria-label="运行模式" value={modelMode} disabled={busy} onChange={event => setModelMode(event.target.value)}>
          <Radio value="offline">离线样例</Radio>
          <Radio value="online">在线 Agent</Radio>
        </Radio.Group>
        <span className="workflow-intro">离线支持库存预占和团队文档原始样例。在线 Agent 使用已配置的模型 API。</span>
      </div>}
      {canCreate && synthetic && <div className="workflow-actions"><Checkbox checked={allowSynthetic} onChange={event => setAllowSynthetic(event.target.checked)}>允许将这份构造样例用于测试运行</Checkbox></div>}
      {snapshot?.stage === "plan_pending_approval" && <div className="workflow-actions"><Checkbox checked={planRead} onChange={event => setPlanRead(event.target.checked)}>我已审阅当前计划及冻结的验收检查</Checkbox><Button type="link" onClick={() => setTab("plan")}>查看计划与检查脚本</Button></div>}
      <div className="workflow-actions">
        {canCreate && <Button type="primary" loading={busy} disabled={busy || !modelMode || !report?.requirement || !report.canSave || (synthetic && !allowSynthetic)} onClick={() => void create()}>{missingWorkflow ? "重新建立运行" : "建立运行并核验需求"}</Button>}
        {!report?.requirement && !workflowId && <span className="workflow-intro">生成设计规格需要有效的 REQ JSON；文本材料请先整理为结构化需求。</span>}
        {next && <Button type="primary" aria-label={next.label} loading={busy} disabled={busy || generating || !permitted || planNeedsRead} onClick={() => void dispatch(next.action, next.tab)}>{next.label}</Button>}
        {retry && <Button loading={busy} disabled={busy} onClick={() => void dispatch(retry)}>重试当前阶段</Button>}
        {workflowId && <Button loading={busy && !next} disabled={busy} icon={<RefreshCw size={15} />} onClick={() => void refresh()}>刷新状态</Button>}
        {consoleUrl && <Button href={consoleUrl}>查看此运行控制台</Button>}
        {canStartOffline && <Button disabled={busy || !report?.canSave} onClick={() => void create("offline")}>用离线样例新建运行</Button>}
        {failedOnline && !retry && <Button disabled={busy || !report?.canSave} onClick={() => void create("online")}>新建在线运行</Button>}
      </div>
      {snapshot?.stage === "execution_blocked" && <p className="workflow-intro">请先在此运行控制台中处理失败任务或恢复暂停，再继续执行；结果未知的请求需要先核对。</p>}
      {generating && <p className="workflow-intro" role="status">
        {designRevision && `自动修订第 ${designRevision.round} 轮（最多 ${designRevision.limit} 轮），本轮处理 ${designRevision.finding_count} 条意见。`}
        {snapshot?.pending_model_role ? `正在等待 ${snapshot.pending_model_role} 返回。` : "正在后台处理。"}
        {elapsed > 0 ? `本阶段已用 ${elapsed} 秒。` : ""}页面会自动更新，也可以手动刷新状态。
      </p>}
    </Card>
    <Card className="workspace-body"><Tabs activeKey={tab} onChange={setTab} items={[{key:"requirement",label:"导入规格",children:requirementContent},{key:"design",label:"设计规格",children:designContent},{key:"plan",label:"任务分解",children:planContent},{key:"execution",label:"编排执行",children:executionContent},{key:"acceptance",label:"验收",children:acceptanceContent}]} /></Card>
  </section>;
}
