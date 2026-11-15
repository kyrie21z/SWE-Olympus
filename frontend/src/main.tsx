import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Layers3,
  LayoutGrid,
  Workflow,
  BookOpen,
  ChevronRight,
  ArrowUpRight,
  Box,
  Check,
  Circle,
  RefreshCw,
  Activity,
  FileJson2,
  ShieldCheck,
  PanelLeftClose,
} from "lucide-react";
import fixture from "../../fixtures/contracts/valid.json";
import scenarios from "../../fixtures/contracts/scenarios.json";
import { parseSnapshot } from "./data/core/events";
import { MockClient } from "./data/mock/client";
import type { Run } from "./contracts/generated";
import "./styles.css";

const stateNames: Record<string, string> = {
  waiting: "待开始",
  ready: "已就绪",
  running: "执行中",
  verifying: "待复核",
  accepted: "已验收",
  failed: "失败",
  blocked: "已阻塞",
  cancelled: "已取消",
};
const scenarioNames: Record<string, string> = {
  normal: "正常推进",
  blocked: "任务阻塞",
  awaiting_review: "等待验收",
  risk_accepted: "人工接受风险",
  stale: "版本过期",
  outcome_unknown: "调用结果未知",
  completed: "全部已验收",
};
const roles = [
  ["Athena", "设计规格"],
  ["Daedalus", "任务规划"],
  ["Hephaestus", "开发执行"],
  ["Momus", "测试与质疑"],
  ["Argus", "独立复核"],
  ["Hermes", "集成交付"],
];

function App() {
  const [page, setPage] = useState("projects");
  const [tab, setTab] = useState("任务详情");
  const [scenario, setScenario] = useState("normal");
  const [run, setRun] = useState<Run>(parseSnapshot(scenarios.normal));
  const [selected, setSelected] = useState("T01");
  const [error, setError] = useState("");
  const [refreshed, setRefreshed] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  async function refresh(name = scenario) {
    try {
      const data = scenarios[name as keyof typeof scenarios];
      const client = new MockClient(parseSnapshot(data));
      setRun(await client.getSnapshot("RUN-campus"));
      setRefreshed(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh(scenario);
  }, [scenario]);
  const accepted = run.tasks.filter(
    (t) => t.state === "accepted" && t.verification_status === "passed",
  ).length;
  const task = fixture.plan.tasks.find((t) => t.task_id === selected)!;
  const runtime = run.tasks.find((t) => t.task_id === selected)!;
  const nav = [
    ["projects", "项目中心", LayoutGrid],
    ["workstation", "工作台", Workflow],
    ["roles", "角色目录", Box],
    ["contracts", "公共契约", FileJson2],
  ] as const;
  return (
    <div className={collapsed ? "app collapsed" : "app"}>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage("projects");
          }}
        >
          <span className="brand-icon">
            <Layers3 size={23} />
          </span>
          <span>
            OLYMPUS<small>Agent Engineering</small>
          </span>
        </a>
        <div className="nav-label">工作空间</div>
        <nav>
          {nav.map(([id, label, Icon]) => (
            <button
              key={id}
              className={page === id ? "nav-item active" : "nav-item"}
              onClick={() => setPage(id)}
              title={label}
            >
              <Icon size={18} />
              <span>{label}</span>
              {page === id && <i />}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <ShieldCheck size={18} />
          <p>
            人掌握关键决定<span>让过程可见，让结果可核对。</span>
          </p>
        </div>
        <div className="sidebar-bottom">
          <span className="avatar">O</span>
          <div>
            课程项目空间<small>P1 · Foundation</small>
          </div>
          <button
            className="icon-button collapse"
            aria-label="折叠导航"
            onClick={() => setCollapsed(!collapsed)}
          >
            <PanelLeftClose size={16} />
          </button>
        </div>
      </aside>
      <div className="main">
        <header>
          <div className="breadcrumb">
            工作空间 <ChevronRight size={14} />{" "}
            <strong>{nav.find((n) => n[0] === page)?.[1]}</strong>
          </div>
          <div className="header-right">
            <span className="env-dot" /> 本地预览 <span className="divider" />
            <a
              href="https://github.com/kyrie21z/SWE-Olympus"
              target="_blank"
              rel="noreferrer"
            >
              GitHub <ArrowUpRight size={14} />
            </a>
            <span className="avatar user">Y</span>
          </div>
        </header>
        <main>
          <div className="eyebrow">SWE–OLYMPUS / P1 FOUNDATION</div>
          <div className="page-heading">
            <div>
              <h1>
                {page === "projects"
                  ? "每一个想法，都有清晰的下一步。"
                  : page === "workstation"
                    ? "项目工作台"
                    : page === "roles"
                      ? "协作，各司其职。"
                      : "一致的契约，可靠的交接。"}
              </h1>
              <p>
                {page === "projects"
                  ? "从确认需求到可验证交付，在同一个工作空间组织 Agent 协作。"
                  : page === "workstation"
                    ? "查看构造样例中的任务、依赖与交付要求。"
                    : page === "roles"
                      ? "角色名负责辨识，输入输出与权限负责约束。"
                      : "所有模块共享同一份数据定义，版本与来源贯穿流程。"}
              </p>
            </div>
            <span className="pill">基础版本 v0.1</span>
          </div>
          <div className="notice">
            <Activity size={16} />
            <span>
              <b>样例预览</b> · 当前数据为人工构造，尚未连接真实 Agent 调度。
            </span>
          </div>
          {error && (
            <div role="alert" className="error">
              {error}
            </div>
          )}
          {page === "projects" && (
            <>
              <div className="stat-grid">
                <div>
                  <span>当前项目</span>
                  <strong>
                    01<small>校园场景</small>
                  </strong>
                </div>
                <div>
                  <span>基础任务</span>
                  <strong>
                    08<small>12 条依赖</small>
                  </strong>
                </div>
                <div>
                  <span>协作角色</span>
                  <strong>
                    06<small>+ Zeus 程序调度</small>
                  </strong>
                </div>
                <div>
                  <span>数据来源</span>
                  <strong className="text-stat">
                    Mock<small>可离线浏览</small>
                  </strong>
                </div>
              </div>
              <div className="section-heading">
                <h2>
                  项目目录 <span>01</span>
                </h2>
                <span>单项目原型</span>
              </div>
              <article className="project-card">
                <div className="project-top">
                  <div className="project-symbol">
                    <Workflow size={25} />
                  </div>
                  <span className="tag">课程构造案例</span>
                </div>
                <h2>校园活动报名与候补</h2>
                <p>
                  满员进入候补，取消后自动递补。以明确的约束和验收记录，连接设计、任务与执行。
                </p>
                <div className="chips">
                  <span>REQ I001-v2</span>
                  <span>DS v1</span>
                  <span>PLAN v1</span>
                </div>
                <div className="project-flow">
                  <span>需求确认</span>
                  <ChevronRight size={13} />
                  <span>设计规格</span>
                  <ChevronRight size={13} />
                  <span>任务计划</span>
                  <ChevronRight size={13} />
                  <span>编排执行</span>
                </div>
                <footer>
                  <div className="mini-avatars">
                    <span>A</span>
                    <span>D</span>
                    <span>H</span>
                    <small>6 个角色</small>
                  </div>
                  <button
                    className="primary"
                    onClick={() => setPage("workstation")}
                  >
                    进入工作台 <ArrowUpRight size={16} />
                  </button>
                </footer>
              </article>
              <div className="principles">
                <h3>先建立基础，再连接执行。</h3>
                <p>
                  公共类型与样例已为后续模块准备就绪。设计生成、任务调度与正式验收将在后续阶段接入。
                </p>
                <button
                  className="text-button"
                  onClick={() => setPage("contracts")}
                >
                  查看公共契约 <ChevronRight size={16} />
                </button>
              </div>
            </>
          )}
          {page === "workstation" && (
            <>
              <div className="workspace-toolbar">
                <div>
                  <Workflow size={18} />
                  <strong>校园活动报名与候补</strong>
                  <span className="tag">RUN-campus</span>
                </div>
                <div>
                  <label htmlFor="scenario">样例场景</label>
                  <select
                    id="scenario"
                    value={scenario}
                    onChange={(e) => setScenario(e.target.value)}
                  >
                    {Object.entries(scenarioNames).map(([id, title]) => (
                      <option key={id} value={id}>
                        {title}
                      </option>
                    ))}
                  </select>
                  <button
                    className="icon-button"
                    aria-label="刷新样例"
                    onClick={() => void refresh()}
                  >
                    <RefreshCw size={16} />
                  </button>
                </div>
              </div>
              <div className="workbench">
                <section className="task-panel">
                  <div className="progress-heading">
                    <span>已验收任务</span>
                    <strong>{accepted} / 8</strong>
                  </div>
                  <progress max={8} value={accepted} />
                  <p className="caption">人工接受风险不计入已验收</p>
                  <div className="task-list">
                    {fixture.plan.tasks.map((t) => {
                      const s = run.tasks.find((r) => r.task_id === t.task_id)!;
                      return (
                        <button
                          key={t.task_id}
                          onClick={() => setSelected(t.task_id)}
                          className={
                            selected === t.task_id ? "task selected" : "task"
                          }
                        >
                          {s.state === "accepted" ? (
                            <Check size={16} />
                          ) : (
                            <Circle size={14} />
                          )}
                          <div>
                            <small>
                              {t.task_id} · {t.executor}
                            </small>
                            <span>{t.goal}</span>
                          </div>
                          <span className={`state-dot ${s.state}`} />
                        </button>
                      );
                    })}
                  </div>
                </section>
                <section className="detail-panel">
                  <div className="tabs">
                    {["任务详情", "依赖清单", "事件记录"].map((t) => (
                      <button
                        key={t}
                        className={tab === t ? "selected" : ""}
                        onClick={() => setTab(t)}
                      >
                        {t}
                      </button>
                    ))}
                  </div>
                  {tab === "任务详情" ? (
                    <div className="task-detail">
                      <div className="detail-title">
                        <span className="eyebrow">
                          {task.task_id} / {task.type.toUpperCase()}
                        </span>
                        <span className={`tag ${runtime.state}`}>
                          {stateNames[runtime.state]}
                        </span>
                      </div>
                      <h2>{task.goal}</h2>
                      <p>
                        任务的目标、产物与验收要求使用公共契约中的同源数据。
                      </p>
                      {runtime.disposition === "risk_accepted" && (
                        <div className="risk">
                          未验证 · 人工接受风险
                          <br />
                          {runtime.disposition_reason}
                        </div>
                      )}
                      {scenario === "outcome_unknown" && selected === "T01" && (
                        <div className="risk">
                          调用结果未知 · 需先核对外部结果，再决定是否重试
                        </div>
                      )}
                      {runtime.verification_status === "stale" && (
                        <div className="risk">上游版本变化 · 需要重新验收</div>
                      )}
                      <dl>
                        <div>
                          <dt>执行角色</dt>
                          <dd>{task.executor}</dd>
                        </div>
                        <div>
                          <dt>独立复核</dt>
                          <dd>{task.reviewer}</dd>
                        </div>
                        <div>
                          <dt>前置任务</dt>
                          <dd>{task.depends_on.join("、") || "无前置依赖"}</dd>
                        </div>
                        <div>
                          <dt>验收状态</dt>
                          <dd>
                            {runtime.verification_status === "passed"
                              ? "已验证（构造样例）"
                              : "尚未验证"}
                          </dd>
                        </div>
                      </dl>
                      <div className="detail-box">
                        <h3>产物与写入范围</h3>
                        <code>{task.outputs.join(", ")}</code>
                        <code>{task.write_scope.join(", ")}</code>
                      </div>
                      <div className="detail-box">
                        <h3>需求来源</h3>
                        <div className="chips">
                          {task.sources.map((s) => (
                            <span key={s}>{s}</span>
                          ))}
                        </div>
                      </div>
                      <div className="handoff-note">
                        <BookOpen size={17} />
                        <span>
                          当前为基础预览。正式执行与人工批准将在调度模块接入后开放。
                        </span>
                      </div>
                    </div>
                  ) : tab === "依赖清单" ? (
                    <div className="table-wrap">
                      <p className="caption">
                        任务 DAG 的数据预览 · 可视化画布由 P6 接入
                      </p>
                      <table>
                        <thead>
                          <tr>
                            <th>任务</th>
                            <th>前置任务</th>
                            <th>产物</th>
                          </tr>
                        </thead>
                        <tbody>
                          {fixture.plan.tasks.map((t) => (
                            <tr key={t.task_id}>
                              <td>{t.task_id}</td>
                              <td>{t.depends_on.join(", ") || "—"}</td>
                              <td>{t.outputs[0]}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="empty-state">
                      <Activity size={34} />
                      <h3>等待接入真实运行事件</h3>
                      <p>当前场景是独立状态夹具，切换场景不会伪造运行事件。</p>
                      <small>
                        最近读取 {refreshed || "—"} · 序号 {run.event_seq}
                      </small>
                    </div>
                  )}
                </section>
              </div>
            </>
          )}
          {page === "roles" && (
            <div className="role-grid">
              {roles.map(([name, role], i) => (
                <article key={name}>
                  <span className="role-index">0{i + 1}</span>
                  <Box size={23} />
                  <h2>{name}</h2>
                  <p>{role}</p>
                  <span className="tag">后续阶段接入</span>
                </article>
              ))}
            </div>
          )}
          {page === "contracts" && (
            <div className="contract-view">
              <div>
                <FileJson2 size={30} />
                <h2>一个来源，多端复用</h2>
                <p>Python 数据模型 → JSON Schema → TypeScript 类型</p>
                <div className="chips">
                  {[
                    "REQ",
                    "DS",
                    "PLAN",
                    "Task",
                    "Run",
                    "Attempt",
                    "Artifact",
                    "Review",
                    "Event",
                    "Command",
                    "Receipt",
                    "ExecutionRecord",
                  ].map((x) => (
                    <span key={x}>{x}</span>
                  ))}
                </div>
              </div>
              <table>
                <tbody>
                  <tr>
                    <th>模式</th>
                    <td>mock / real_tool / real_model</td>
                  </tr>
                  <tr>
                    <th>版本</th>
                    <td>schema_version 1.0</td>
                  </tr>
                  <tr>
                    <th>事件</th>
                    <td>按序号追加，重复去重，缺口补取</td>
                  </tr>
                  <tr>
                    <th>确认</th>
                    <td>绑定对象版本与内容指纹</td>
                  </tr>
                  <tr>
                    <th>工作流</th>
                    <td>LangGraph 承载后续阶段控制</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
          <div className="page-footer">
            <span>OLYMPUS · 让协作有迹可循</span>
            <span>P1 基础交付 / 示例数据</span>
          </div>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
