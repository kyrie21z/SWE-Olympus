import { useEffect, useState } from "react";
import { Alert, Button, Card, Checkbox, Input, InputNumber, Space, Spin, Tag } from "antd";
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
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ok: boolean; message: string} | null>(null);
  useEffect(() => {
    setBaseUrl(setting.base_url); setModel(setting.model); setTimeout(setting.timeout_seconds);
    setKey(""); setClearKey(false);
  }, [setting]);
  const role = roles.find(item => item.name === setting.role_id)!;
  const dirty = baseUrl !== setting.base_url || model !== setting.model || timeout !== setting.timeout_seconds || Boolean(key) || clearKey;
  async function save() {
    setBusy("save"); setError(""); setResult(null);
    try {
      const saved = await workflowClient.saveAgent(setting.role_id, agentUpdate(baseUrl, model, timeout, key, clearKey));
      onSaved(saved); setKey(""); setClearKey(false);
      setResult({ok: true, message: "配置已保存到本机后端。"});
    } catch (error) { setError(describeApiError(error)); }
    finally { setBusy(null); }
  }
  async function test() {
    setBusy("test"); setError(""); setResult(null);
    try { setResult(await workflowClient.testAgent(setting.role_id)); }
    catch (error) { setError(describeApiError(error)); }
    finally { setBusy(null); }
  }
  return <Card className="agent-api-card" data-testid={`agent-settings-${setting.role_id}`}>
    <div className="agent-api-heading"><div><h2>{setting.role_id}</h2><span>{role.title}</span></div><Tag color={setting.configured ? "success" : "default"}>{setting.configured ? "已配置" : "未配置"}</Tag></div>
    <p>{role.text}</p>
    <form onSubmit={event => {event.preventDefault(); void save();}} className="agent-api-fields" aria-label={`${setting.role_id} API 设置`}>
      <label htmlFor={`${setting.role_id}-base`}>API 根地址</label>
      <Input id={`${setting.role_id}-base`} value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" autoComplete="off" required />
      <label htmlFor={`${setting.role_id}-model`}>模型名称</label>
      <Input id={`${setting.role_id}-model`} value={model} onChange={event => setModel(event.target.value)} placeholder="提供方的 model 标识" autoComplete="off" required />
      <label htmlFor={`${setting.role_id}-key`}>API Key</label>
      <Input.Password id={`${setting.role_id}-key`} value={key} onChange={event => {setKey(event.target.value); if (event.target.value) setClearKey(false);}} placeholder={setting.has_api_key ? "已保存密钥；留空保留" : "输入此角色使用的密钥"} autoComplete="new-password" />
      <div className="agent-api-options"><label htmlFor={`${setting.role_id}-timeout`}>超时秒数</label><InputNumber id={`${setting.role_id}-timeout`} min={1} max={300} precision={0} value={timeout} onChange={value => setTimeout(value ?? 120)} /></div>
      <Checkbox checked={clearKey} disabled={Boolean(key) || !setting.has_api_key} onChange={event => setClearKey(event.target.checked)}>清除已保存密钥</Checkbox>
      {error && <Alert type="error" showIcon title={error} />}
      {result && <Alert type={result.ok ? "success" : "error"} showIcon title={result.message} />}
      <Space wrap>
        <Button type="primary" htmlType="submit" loading={busy === "save"} disabled={busy === "test"} icon={<Save size={15} />}>保存配置</Button>
        <Button onClick={() => void test()} loading={busy === "test"} disabled={busy === "save" || !setting.configured || dirty} icon={<PlugZap size={15} />}>测试连接</Button>
      </Space>
      <small>先保存，再测试连接。测试会向该接口发送一次最小模型请求，可能产生费用。</small>
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
    {settings && !settings.some(setting => setting.configured) && <Alert type="info" showIcon title="当前默认使用离线模式" description="可在工作台直接运行库存预占和团队文档样例。完整保存任一模型接口后，新运行会使用在线模式。" />}
    {error && <Alert type="error" showIcon title={error} />}
    {loading && !settings && <Spin />}
    <div className="agent-settings-grid">{settings?.map(setting => <AgentCard key={setting.role_id} setting={setting} onSaved={updated => setSettings(previous => previous?.map(item => item.role_id === updated.role_id ? updated : item) ?? [updated])} />)}</div>
  </section>;
}
