import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Collapse, Descriptions, Empty, Space, Spin, Steps, Table, Tabs, Tag } from "antd";
import { Download, RefreshCw, Settings2, Workflow } from "lucide-react";
import type { Requirement } from "../contracts/generated";
import { InspectionPanel, RequirementDetails } from "../imports/Preview";
import { downloadText, type ImportedRequirement, type Inspection } from "../imports/model";
import { describeApiError, workflowClient, type WorkflowAction, type WorkflowSnapshot } from "./client";
import { BUSY_STAGES, NEXT_ACTION, readableJson, STAGES, stageIndex } from "./model";
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
function Review({value}: {value: unknown}) {
  if (!value) return null;
  const outer = jsonObject(value);
  const review = jsonObject(outer.review ?? value);
  const verdict = typeof review.verdict === "string" ? review.verdict : "已返回报告";
  const findings = Array.isArray(review.findings) ? review.findings : [];
  return <div className="workflow-review"><Space wrap><strong>独立审查</strong><Tag color={verdict === "passed" ? "success" : "warning"}>{verdict}</Tag></Space>
    {findings.length > 0 && <ul>{findings.map((finding, i) => <li key={i}>{typeof finding === "string" ? finding : readableJson(finding)}</li>)}</ul>}
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
  const [planRead, setPlanRead] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [loadError, setLoadError] = useState("");
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
          setSnapshot(next); setLoadError("");
          if (next.retry_safe) pendingOperation.current = null;
        }
      } catch (error) {if (!controller.signal.aborted) setLoadError(describeApiError(error));}
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 2000);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [workflowId]);
  async function refresh() {
    if (!workflowId || busy) return;
    setBusy(true);
    try {setSnapshot(await workflowClient.get(workflowId)); setLoadError("");}
    catch (error) {setLoadError(describeApiError(error));}
    finally {if (mounted.current) setBusy(false);}
  }
  async function create() {
    if (!report?.requirement || !report.canSave || busy) return;
    setBusy(true); setActionError(""); epoch.current++;
    try {
      const requirement = JSON.parse(file.sourceText.replace(/^\uFEFF/, "")) as Requirement;
      const next = await workflowClient.create(requirement, allowSynthetic);
      if (!mounted.current) return;
      setSnapshot(next); setWorkflowId(next.workflow_id);
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
      setSnapshot(next); pendingOperation.current = null;
    } catch (error) {if (mounted.current) setActionError(describeApiError(error));}
    finally {if (mounted.current) setBusy(false);}
  }
  const synthetic = report?.status === "synthetic";
  const generating = snapshot ? BUSY_STAGES.has(snapshot.stage) : false;
  const next = snapshot ? NEXT_ACTION[snapshot.stage] : undefined;
  const permitted = !snapshot?.allowed_actions || (next && snapshot.allowed_actions.includes(next.action));
  const retry = snapshot?.retry_safe && snapshot.retry_action && snapshot.allowed_actions?.includes(snapshot.retry_action) ? snapshot.retry_action : null;
  const executionRows = snapshot ? Array.isArray(snapshot.executions) ? snapshot.executions : Object.values(snapshot.executions ?? {}) : [];
  const consoleUrl = snapshot ? `?run=${encodeURIComponent(snapshot.run_id)}#console` : "#console";
  const planNeedsRead = next?.action === "approve_plan" && !planRead;
  const requirementContent = <>
    <div className="section-toolbar"><div><h3>需求交接内容</h3><p>原文件只读，补充说明独立保存。</p></div><Space wrap><Button icon={<Download size={15} />} onClick={() => downloadText(file.fileName, file.sourceText)}>导出原文件</Button><Button onClick={onNewImport}>导入新版本</Button></Space></div>
    {report ? <><InspectionPanel report={report} /><RequirementDetails file={file} report={report} /><Collapse items={[{key: "raw", label: "查看原始文件", children: <pre className="raw-source">{file.sourceText}</pre>}]} /></> : <Spin />}
  </>;
  const designContent = snapshot?.design ? <div className="workflow-artifact">
    <Descriptions size="small" column={1} items={[{key:"id",label:"设计",children:`${snapshot.design.object_id} · v${snapshot.design.version}`},{key:"hash",label:"内容指纹",children:<code className="workflow-key">{snapshot.design.content_hash}</code>},{key:"concurrency",label:"并发约定",children:snapshot.design.content.concurrency},{key:"interaction",label:"交互",children:snapshot.design.content.interaction}]} />
    <Space wrap>{snapshot.design.content.modules.map(value => <Tag key={value}>{value}</Tag>)}</Space>
    <Table rowKey="id" pagination={false} size="small" scroll={{x:700}} dataSource={snapshot.design.content.elements} columns={[{title:"设计元素",dataIndex:"id"},{title:"需求来源",dataIndex:"sources",render:(value:string[])=>value.join("、")},{title:"设计说明",dataIndex:"description"},{title:"验收方式",dataIndex:"acceptance_method"}]} />
    <Review value={snapshot.design_review} /><Collapse items={[{key:"design",label:"查看完整设计正文",children:<pre className="workflow-json">{readableJson(snapshot.design)}</pre>}]} />
  </div> : <Pending title="设计规格尚未生成" description="接纳需求后即可生成设计。未配置模型 API 时，工作台使用匹配的离线样例。" />;
  const planContent = snapshot?.plan ? <div className="workflow-artifact">
    <Descriptions size="small" column={1} items={[{key:"id",label:"计划",children:`${snapshot.plan.object_id} · v${snapshot.plan.version}`},{key:"hash",label:"内容指纹",children:<code className="workflow-key">{snapshot.plan.content_hash}</code>}]} />
    <Table rowKey="task_id" pagination={false} size="small" scroll={{x:850}} dataSource={snapshot.plan.tasks} columns={[{title:"任务",dataIndex:"task_id",width:80},{title:"目标",dataIndex:"goal"},{title:"依赖",dataIndex:"depends_on",render:(value:string[])=>value.join("、")||"无"},{title:"执行者",dataIndex:"executor"},{title:"复核者",dataIndex:"reviewer"},{title:"检查",dataIndex:"checks",render:(value:string[])=>value.join("、")}]} />
    <Review value={snapshot.plan_review} /><FrozenChecks acceptance={snapshot.acceptance} />
    <Collapse items={[{key:"plan",label:"查看完整任务计划",children:<pre className="workflow-json">{readableJson(snapshot.plan)}</pre>}]} />
  </div> : <Pending title="任务计划尚未生成" description="批准当前设计后，由 Daedalus 规划任务，Momus 审查并生成独立验收检查。" />;
  const executionContent = <div className="workflow-artifact">
    {!snapshot && <Pending title="P6 运行控制台已接入" description="建立并批准本需求的计划后，在这里执行全部任务或查看对应运行控制台。" />}
    {snapshot && <Alert type={snapshot.stage === "execution_blocked" ? "warning" : "info"} showIcon title="执行状态以实际检查和独立复核为准" description="执行范围来自批准的计划；每项任务只有通过冻结检查和 Argus 复核后才能接纳。" />}
    <Space wrap><Button type="primary" href={consoleUrl}>打开运行控制台</Button>{snapshot && <Tag>{snapshot.run_id}</Tag>}</Space>
    {executionRows.length ? <Collapse items={executionRows.map((execution, i) => {const row = jsonObject(execution); return {key:String(i),label:`任务执行证据 · ${String(row.task_id ?? jsonObject(row.execution).task_id ?? i + 1)}`,children:<ExecutionEvidence value={execution} />};})} /> : snapshot && <Empty description="当前还没有实际执行证据" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
  </div>;
  const acceptanceContent = snapshot?.stage === "completed" ? <Alert type="success" showIcon title="当前运行已完成人工最终验收" description={`运行编号：${snapshot.run_id}。可在执行页和运行控制台查看证据。`} /> : <><Pending title={snapshot?.stage === "awaiting_acceptance" ? "全部必要任务已接纳，等待最终验收" : "暂无验收结果"} description="阅读实际检查、独立复核及任务执行证据后，再通过上方按钮确认最终验收。" />{snapshot && <Button onClick={() => setTab("execution")}>查看任务执行证据</Button>}</>;
  return <section className="workflow-workspace" data-testid="workflow-workspace">
    <Card title={<Space><Workflow size={18} />需求到执行</Space>} extra={<Button icon={<Settings2 size={15} />} onClick={onSettings}>Agent API 设置</Button>}>
      <div className="workflow-status-heading"><div><h3 data-testid="workflow-stage">{snapshot ? STAGES[snapshot.stage] ?? snapshot.stage : workflowId ? "正在恢复运行" : "建立当前需求的运行"}</h3><p>{snapshot ? <><span className="workflow-key">{snapshot.workflow_id}</span> · {snapshot.run_id}</> : "需求、设计、计划和最终验收均由你明确确认。"}</p></div>{generating && <Spin size="small" />}</div>
      <Steps className="workflow-steps" size="small" current={snapshot ? stageIndex(snapshot.stage) : 0} status={snapshot?.stage === "error" || snapshot?.stage.endsWith("_blocked") ? "error" : snapshot?.stage === "completed" ? "finish" : "process"} items={[{title:"需求接纳"},{title:"设计与审查"},{title:"计划与检查"},{title:"受控执行"},{title:"最终验收"}]} />
      {synthetic && <Alert type="info" showIcon title="构造测试需求" description="本次运行继续保留构造来源标识；测试确认不代表真实用户的生产授权。" />}
      {snapshot?.model_mode === "offline" && <Alert type="info" showIcon title="离线模式" description="使用与需求版本匹配的预置 Agent 响应。Python 检查在本机实际执行。" />}
      {actionError && <Alert type="error" showIcon title={actionError} />}
      {loadError && <Alert type="warning" showIcon title="运行数据可能过期" description={loadError} />}
      {storageError && <Alert type="warning" showIcon title={storageError} />}
      {snapshot?.error && <Alert type="error" showIcon title={`${snapshot.error.code}：${snapshot.error.message}`} action={<Button onClick={onSettings}>检查 API 设置</Button>} />}
      {!workflowId && synthetic && <div className="workflow-actions"><Checkbox checked={allowSynthetic} onChange={event => setAllowSynthetic(event.target.checked)}>允许将这份构造样例用于测试运行</Checkbox></div>}
      {snapshot?.stage === "plan_pending_approval" && <div className="workflow-actions"><Checkbox checked={planRead} onChange={event => setPlanRead(event.target.checked)}>我已审阅当前计划及冻结的验收检查</Checkbox><Button type="link" onClick={() => setTab("plan")}>查看计划与检查脚本</Button></div>}
      <div className="workflow-actions">
        {!workflowId && <Button type="primary" loading={busy} disabled={busy || !report?.requirement || !report.canSave || (synthetic && !allowSynthetic)} onClick={() => void create()}>建立运行并核验需求</Button>}
        {!report?.requirement && !workflowId && <span className="workflow-intro">生成设计规格需要有效的 REQ JSON；文本材料请先整理为结构化需求。</span>}
        {next && <Button type="primary" aria-label={next.label} loading={busy} disabled={busy || generating || !permitted || planNeedsRead} onClick={() => void dispatch(next.action, next.tab)}>{next.label}</Button>}
        {retry && <Button loading={busy} disabled={busy} onClick={() => void dispatch(retry)}>重试当前阶段</Button>}
        {workflowId && <Button loading={busy && !next} disabled={busy || generating} icon={<RefreshCw size={15} />} onClick={() => void refresh()}>刷新状态</Button>}
        {snapshot && <Button href={consoleUrl}>查看此运行控制台</Button>}
      </div>
      {snapshot?.stage === "execution_blocked" && <p className="workflow-intro">请先在此运行控制台中处理失败任务或恢复暂停，再继续执行；结果未知的请求需要先核对。</p>}
      {generating && <p className="workflow-intro">正在后台运行，可以离开页面；页面会自动补取状态。请勿因等待时间较长重复提交模型请求。</p>}
    </Card>
    <Card className="workspace-body"><Tabs activeKey={tab} onChange={setTab} items={[{key:"requirement",label:"导入规格",children:requirementContent},{key:"design",label:"设计规格",children:designContent},{key:"plan",label:"任务分解",children:planContent},{key:"execution",label:"编排执行",children:executionContent},{key:"acceptance",label:"验收",children:acceptanceContent}]} /></Card>
  </section>;
}
