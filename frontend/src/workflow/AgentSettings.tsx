import { useEffect, useState } from "react";
import { Alert, Button, Card, Checkbox, Input, InputNumber, Select, Space, Spin, Tag } from "antd";
import { RefreshCw, Save, PlugZap } from "lucide-react";
import { roles } from "../shell-config";
import { AGENT_ROLES, describeApiError, workflowClient, type AgentSetting } from "./client";
import { agentUpdate } from "./model";
import "./workflow.css";

function AgentCard({setting, onSaved}: {setting: AgentSetting; onSaved: (setting: AgentSetting) => void}) {
  const [baseUrl, setBaseUrl] = useState(setting.base_url);
  const [model, setModel] = useState(setting.model);
  const [timeout, setTimeout] = useState(setting.timeout_seconds);
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState<"save" | "test" | "models" | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<Awaited<ReturnType<typeof workflowClient.testAgent>> | null>(null);
  const [didSave, setDidSave] = useState(false);
  useEffect(() => {
    setBaseUrl(setting.base_url); setModel(setting.model); setTimeout(setting.timeout_seconds);
    setKey(""); setClearKey(false);
  }, [setting]);
  const role = roles.find(item => item.name === setting.role_id)!;
  const dirty = baseUrl !== setting.base_url || model !== setting.model || timeout !== setting.timeout_seconds || Boolean(key) || clearKey;
  useEffect(() => {
    if (dirty) { setTestResult(null); setDidSave(false); }
  }, [dirty]);
  const canList = Boolean(baseUrl.trim() && (key.trim() || (setting.has_api_key && baseUrl.trim().replace(/\/+$/, "") === setting.base_url))) && !clearKey;
  async function listModels() {
    setBusy("models"); setError(""); setTestResult(null); setDidSave(false); setModels([]);
    try {
      const {models: available} = await workflowClient.listModels(setting.role_id, baseUrl, key);
      if (available.length) setModels(available);
      else setError("提供方返回了空列表，请手动填写模型 ID 或检查账号权限。");
    } catch (error) { setError(describeApiError(error)); }
    finally { setBusy(null); }
  }
  async function save() {
    if (busy) return;
    setBusy("save"); setError(""); setTestResult(null); setDidSave(false);
    try {
      const saved = await workflowClient.saveAgent(setting.role_id, agentUpdate(baseUrl, model, timeout, key, clearKey));
      onSaved(saved); setKey(""); setClearKey(false);
      setDidSave(true);
    } catch (error) { setError(describeApiError(error)); }
    finally { setBusy(null); }
  }
  async function test() {
    setBusy("test"); setError(""); setTestResult(null); setDidSave(false);
    try { setTestResult(await workflowClient.testAgent(setting.role_id)); }
    catch (error) { setError(describeApiError(error)); }
    finally { setBusy(null); }
  }
  return <Card className="agent-api-card" data-testid={`agent-settings-${setting.role_id}`}>
    <div className="agent-api-heading"><div><h2>{setting.role_id}</h2><span>{role.title}</span></div><Tag color={setting.configured ? "success" : "default"}>{setting.configured ? "已配置" : "未配置"}</Tag></div>
    <p>{role.text}</p>
    <form onSubmit={event => {event.preventDefault(); void save();}} className="agent-api-fields" aria-label={`${setting.role_id} API 设置`}>
      <label htmlFor={`${setting.role_id}-base`}>API 根地址</label>
      <Input id={`${setting.role_id}-base`} value={baseUrl} disabled={Boolean(busy)} onChange={event => {setBaseUrl(event.target.value); setModels([]);}} placeholder="https://api.example.com/v1" autoComplete="off" required />
      <label htmlFor={`${setting.role_id}-model`}>模型名称</label>
      <Input id={`${setting.role_id}-model`} value={model} onChange={event => setModel(event.target.value)} placeholder="选择或填写提供方的模型 ID" autoComplete="off" required />
      {models.length > 0 && <Select aria-label="从模型列表选择" placeholder="选择模型 ID" showSearch value={models.includes(model) ? model : undefined} options={models.map(id => ({value: id, label: id}))} onChange={setModel} />}
      <label htmlFor={`${setting.role_id}-key`}>API Key</label>
      <Input.Password id={`${setting.role_id}-key`} value={key} disabled={Boolean(busy)} onChange={event => {setKey(event.target.value); setModels([]); if (event.target.value) setClearKey(false);}} placeholder={setting.has_api_key ? "已保存密钥；留空保留" : "输入此角色使用的密钥"} autoComplete="new-password" />
      <small>{setting.has_api_key ? "后端已保存密钥，页面隐藏其内容。测试连接使用这把已保存的密钥。" : "当前没有已保存的密钥。填写并保存后可测试连接。"}</small>
      <div className="agent-api-options"><label htmlFor={`${setting.role_id}-timeout`}>超时秒数</label><InputNumber id={`${setting.role_id}-timeout`} min={1} max={300} precision={0} value={timeout} onChange={value => setTimeout(value ?? 120)} /></div>
      <Checkbox checked={clearKey} disabled={Boolean(busy) || Boolean(key) || !setting.has_api_key} onChange={event => {setClearKey(event.target.checked); setModels([]);}}>清除已保存密钥</Checkbox>
      {error && <Alert type="error" showIcon title={error} />}
      {didSave && !dirty && <Alert type="success" showIcon title="配置已保存到本机后端。" />}
      {testResult && !dirty && <Alert type={testResult.ok ? "success" : "error"} showIcon title={testResult.message}
        description={`使用已保存密钥${testResult.model ? `调用 ${testResult.model}` : ""}${typeof testResult.elapsed_ms === "number" ? `，耗时 ${(testResult.elapsed_ms / 1_000).toFixed(1)} 秒` : ""}。这是短请求测试。`} />}
      <Space wrap>
        <Button onClick={() => void listModels()} loading={busy === "models"} disabled={Boolean(busy) || !canList}>获取模型列表</Button>
        <Button type="primary" htmlType="submit" loading={busy === "save"} disabled={Boolean(busy)} icon={<Save size={15} />}>保存配置</Button>
        <Button onClick={() => void test()} loading={busy === "test"} disabled={Boolean(busy) || !setting.configured || dirty} icon={<PlugZap size={15} />}>测试连接</Button>
      </Space>
      <small>填写地址和密钥后可获取模型列表。选好模型后保存并测试连接，测试会发送一次最小模型请求。</small>
    </form>
  </Card>;
}

export function AgentSettings() {
  const [settings, setSettings] = useState<AgentSetting[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  async function load(signal?: AbortSignal) {
    setLoading(true); setError("");
    try {
      const result = await workflowClient.getAgents(signal);
      if (!signal?.aborted) setSettings(AGENT_ROLES.map(role => result.agents.find(item => item.role_id === role) ?? {role_id: role, base_url: "", model: "", timeout_seconds: 120, configured: false, has_api_key: false}));
    } catch (error) { if (!signal?.aborted) setError(describeApiError(error)); }
    finally { if (!signal?.aborted) setLoading(false); }
  }
  useEffect(() => {const abort = new AbortController(); void load(abort.signal); return () => abort.abort();}, []);
  return <section className="agent-settings" data-testid="agent-api-settings">
    <Alert type="info" showIcon title="为每个 Agent 独立选择 API 和模型" description="支持 OpenAI 兼容的 Chat Completions 接口，根地址后自动追加 /chat/completions。密钥保存在本机后端，不写入浏览器存储，查询只返回是否已保存。Zeus 负责程序编排，YOU 负责人工批准，无需模型接口。" />
    <div className="agent-settings-toolbar"><strong>角色与模型接口</strong><Button icon={<RefreshCw size={15} />} loading={loading} onClick={() => void load()}>刷新配置</Button></div>
    {settings && !settings.some(setting => setting.configured) && <Alert type="info" showIcon title="还没有可用的在线模型配置" description="可在工作台直接离线运行库存预占和团队文档样例。自定义需求从 Athena 和 Momus 的配置开始。" />}
    {error && <Alert type="error" showIcon title={error} />}
    {loading && !settings && <Spin />}
    <div className="agent-settings-grid">{settings?.map(setting => <AgentCard key={setting.role_id} setting={setting} onSaved={updated => setSettings(previous => previous?.map(item => item.role_id === updated.role_id ? updated : item) ?? [updated])} />)}</div>
  </section>;
}
