import { useEffect, useState } from "react";
import {
  App,
  Avatar,
  Badge,
  Breadcrumb,
  Button,
  Card,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Menu,
  Modal,
  Select,
  Space,
  Steps,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Bot,
  Boxes,
  Check,
  ChevronRight,
  CircleHelp,
  ClipboardList,
  FileText,
  FolderOpen,
  GitBranch,
  Github,
  Home,
  Layers3,
  Menu as MenuIcon,
  MessageSquare,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Waypoints,
  Workflow,
  Pencil,
  Download,
  Clock3,
} from "lucide-react";
import {
  createDraft,
  type DraftInput,
  type ProjectDraft,
} from "./projects/model";
import { draftStorageLabel, loadDrafts, saveDrafts } from "./projects/storage";

type Page = "home" | "projects" | "workstation" | "agents" | "guide";
const pages = {
  home: "欢迎总览",
  projects: "项目中心",
  workstation: "项目工作台",
  agents: "协作角色",
  guide: "使用指南",
};
const navItems = [
  { key: "home", icon: <Home size={18} />, label: pages.home },
  {
    type: "group" as const,
    label: "项目空间",
    children: [
      {
        key: "projects",
        icon: <FolderOpen size={18} />,
        label: pages.projects,
      },
      {
        key: "workstation",
        icon: <Workflow size={18} />,
        label: pages.workstation,
      },
    ],
  },
  {
    type: "group" as const,
    label: "协作与资源",
    children: [
      { key: "agents", icon: <Bot size={18} />, label: pages.agents },
      { key: "guide", icon: <BookOpen size={18} />, label: pages.guide },
    ],
  },
];
const roles = [
  {
    name: "Athena",
    title: "设计规格",
    icon: Layers3,
    color: "blue",
    text: "将已确认的需求整理为设计边界、模块与接口，保留授权来源。",
  },
  {
    name: "Daedalus",
    title: "任务规划",
    icon: Waypoints,
    color: "purple",
    text: "拆分任务，建立依赖与验收条件，让每一项工作有明确的输入和输出。",
  },
  {
    name: "Hephaestus",
    title: "开发执行",
    icon: Boxes,
    color: "orange",
    text: "按任务契约实施变更，提交可复核的产物与执行记录。",
  },
  {
    name: "Momus",
    title: "测试与质疑",
    icon: MessageSquare,
    color: "magenta",
    text: "检查边界、失败路径与关键假设，对未解决的问题持续保留异议。",
  },
  {
    name: "Argus",
    title: "独立复核",
    icon: ShieldCheck,
    color: "cyan",
    text: "依据验收证据独立检查产物，执行者不能自行批准结果。",
  },
  {
    name: "Hermes",
    title: "集成交付",
    icon: GitBranch,
    color: "green",
    text: "组织产物交接与集成记录，让变更、验证与交付相互关联。",
  },
];
const guideItems = [
  {
    key: "1",
    label: "可以输入什么样的项目？",
    children:
      "任何软件项目需求都可以。说明服务谁、解决什么问题、核心操作是什么，以及时间、平台等约束。无需套用固定业务案例。",
  },
  {
    key: "2",
    label: "创建草稿后会自动生成内容吗？",
    children:
      "当前版本支持需求录入、编辑、导出和项目导航。模型生成服务接入后，才会产生讨论、需求规格、设计与任务；此刻的流程图只是待执行的流程预览。",
  },
  {
    key: "3",
    label: "谁来确认需求和执行结果？",
    children:
      "Agent 提供分析、方案和独立复核，真人确认关键事实、价值取舍与最终结果。草稿与模型输出都不等于已确认需求。",
  },
  {
    key: "4",
    label: "需求保存在哪里？",
    children: `${draftStorageLabel}。当前不跨设备同步；可以导出 JSON 备份。`,
  },
];
function getPage(): Page {
  const value = window.location.hash.slice(1).split("/")[0];
  return Object.hasOwn(pages, value) ? (value as Page) : "home";
}
const dateText = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

function FlowPreview({ captured = false }: { captured?: boolean }) {
  const stages = [
    {
      icon: MessageSquare,
      title: "需求输入",
      subtitle: captured ? "已保存草稿" : "描述你的想法",
      active: true,
    },
    { icon: Bot, title: "多体讨论", subtitle: "澄清与方案评审" },
    { icon: FileText, title: "需求规格", subtitle: "真人确认" },
    { icon: Workflow, title: "设计与编排", subtitle: "任务与依赖" },
  ];
  return (
    <div className="flow-preview" aria-label="待执行的工作流程">
      {stages.map(({ icon: Icon, title, subtitle, active }, index) => (
        <div className="flow-stage-wrap" key={title}>
          <div className={`flow-stage ${active ? "flow-active" : ""}`}>
            <span className="flow-icon">
              <Icon size={20} />
            </span>
            <strong>{title}</strong>
            <span>{subtitle}</span>
          </div>
          {index < stages.length - 1 && (
            <ChevronRight className="flow-arrow" size={18} />
          )}
        </div>
      ))}
    </div>
  );
}

export function OlympusApp() {
  const { message } = App.useApp();
  const [initial] = useState(loadDrafts);
  const [drafts, setDrafts] = useState<ProjectDraft[]>(initial.drafts);
  const [page, setPage] = useState<Page>(getPage);
  const [selectedId, setSelectedId] = useState<string>();
  const [mobileNav, setMobileNav] = useState(false);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState("input");
  const [editing, setEditing] = useState<ProjectDraft>();
  const [help, setHelp] = useState(false);
  const [form] = Form.useForm<DraftInput>();
  const [editForm] = Form.useForm<DraftInput>();
  const current = drafts.find((draft) => draft.id === selectedId) ?? drafts[0];
  useEffect(() => {
    const change = () => setPage(getPage());
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    document.title = `${pages[page]} · SWE-Olympus`;
    window.scrollTo(0, 0);
  }, [page]);
  function navigate(next: Page) {
    setPage(next);
    window.location.hash = next;
    setMobileNav(false);
  }
  function openProject(draft: ProjectDraft) {
    setSelectedId(draft.id);
    setTab("input");
    navigate("workstation");
  }
  function persist(next: ProjectDraft[]): boolean {
    try {
      saveDrafts(next);
      setDrafts(next);
      return true;
    } catch (error) {
      void message.error(
        error instanceof Error ? error.message : "保存失败，请导出备份后重试。",
      );
      return false;
    }
  }
  function submit(input: DraftInput) {
    const draft = createDraft(input);
    if (persist([draft, ...drafts])) {
      form.resetFields();
      openProject(draft);
      void message.success("需求草稿已保存");
    }
  }
  function newProject() {
    navigate("home");
    window.setTimeout(() => document.getElementById("new_prompt")?.focus(), 0);
  }
  function exportDraft(draft: ProjectDraft) {
    const url = URL.createObjectURL(
      new Blob(
        [
          JSON.stringify(
            { type: "olympus_prompt_draft", schemaVersion: 1, draft },
            null,
            2,
          ),
        ],
        { type: "application/json;charset=utf-8" },
      ),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `olympus-draft-${draft.id}.json`;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const draftFields = (
    <>
      <Form.Item
        name="name"
        label="项目名称"
        extra="选填，留空时将使用需求首行命名。"
        rules={[{ max: 80, message: "项目名称最多 80 字" }]}
      >
        <Input placeholder="为你的下一个项目起个名字" maxLength={80} />
      </Form.Item>
      <Form.Item
        name="prompt"
        label="需求描述"
        rules={[
          { required: true, whitespace: true, message: "请先描述你想做的软件" },
          { max: 12000, message: "需求描述最多 12000 字" },
        ]}
      >
        <Input.TextArea
          placeholder={
            "我想做一个……，主要面向……，帮助他们……。\n希望支持的核心操作是……，需要注意的限制是……。"
          }
          autoSize={{ minRows: 5, maxRows: 12 }}
          maxLength={12000}
          showCount
        />
      </Form.Item>
      <Form.Item
        name="constraints"
        label="约束与补充"
        rules={[{ max: 2000, message: "约束与补充最多 2000 字" }]}
      >
        <Input.TextArea
          placeholder="选填，例如使用平台、交付时间、团队规模或技术偏好"
          autoSize={{ minRows: 2, maxRows: 6 }}
          maxLength={2000}
        />
      </Form.Item>
    </>
  );
  const columns = [
    {
      title: "项目名称",
      dataIndex: "name",
      key: "name",
      render: (_: string, draft: ProjectDraft) => (
        <button className="project-name" onClick={() => openProject(draft)}>
          <span className="project-glyph">
            <FolderOpen size={19} />
          </span>
          <span>
            <strong>{draft.name}</strong>
            <small>{draft.prompt.replace(/\s+/g, " ").slice(0, 64)}</small>
          </span>
        </button>
      ),
    },
    {
      title: "当前阶段",
      key: "status",
      width: 120,
      render: () => <Tag color="blue">需求草稿</Tag>,
    },
    {
      title: "最近更新",
      key: "updated",
      width: 145,
      render: (_: unknown, draft: ProjectDraft) => (
        <span className="muted">{dateText(draft.updatedAt)}</span>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 85,
      render: (_: unknown, draft: ProjectDraft) => (
        <Button type="link" onClick={() => openProject(draft)}>
          打开 <ArrowUpRight size={14} />
        </Button>
      ),
    },
  ];
  const projectTable = (limit?: number) => (
    <Table
      rowKey="id"
      columns={columns}
      dataSource={drafts
        .filter((draft) =>
          `${draft.name} ${draft.prompt}`
            .toLowerCase()
            .includes(search.toLowerCase()),
        )
        .slice(0, limit)}
      pagination={limit ? false : { pageSize: 6, hideOnSinglePage: true }}
      scroll={{ x: 600 }}
      locale={{
        emptyText: (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              drafts.length
                ? "没有匹配的项目"
                : "还没有项目，从上方描述一个想法开始"
            }
          />
        ),
      }}
    />
  );
  const navigation = (
    <>
      <div className="workspace-label">
        <span className="workspace-monogram">O</span>
        <div>
          <strong>Olympus Workspace</strong>
          <small>软件工程协作空间</small>
        </div>
      </div>
      <Menu
        mode="inline"
        selectedKeys={[page]}
        items={navItems}
        onClick={({ key }) => navigate(key as Page)}
      />
      <div className="sider-bottom">
        <ShieldCheck size={18} />
        <span>
          人掌握关键决定<small>每一步都可追溯</small>
        </span>
      </div>
    </>
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <Button
          className="mobile-menu"
          type="text"
          aria-label="打开导航"
          icon={<MenuIcon size={20} />}
          onClick={() => setMobileNav(true)}
        />
        <a className="brand" href="#home" onClick={() => navigate("home")}>
          <span className="brand-mark">
            <Layers3 size={23} />
          </span>
          <strong>SWE-Olympus</strong>
          <span className="brand-caption">CONSOLE</span>
        </a>
        <div className="topbar-actions">
          <Tag
            className="environment-tag"
            icon={<span className="status-dot" />}
          >
            本地工作空间
          </Tag>
          <Tooltip title="使用帮助">
            <Button
              type="text"
              aria-label="使用帮助"
              icon={<CircleHelp size={19} />}
              onClick={() => setHelp(true)}
            />
          </Tooltip>
          <Tooltip title="项目仓库">
            <Button
              type="text"
              aria-label="项目仓库"
              href="https://github.com/kyrie21z/SWE-Olympus"
              target="_blank"
              rel="noreferrer"
              icon={<Github size={19} />}
            />
          </Tooltip>
          <span className="topbar-divider" />
          <Avatar size={30} className="user-avatar">
            O
          </Avatar>
          <span className="user-name">项目成员</span>
        </div>
      </header>
      <aside className="sider">{navigation}</aside>
      <Drawer
        title="SWE-Olympus"
        placement="left"
        open={mobileNav}
        onClose={() => setMobileNav(false)}
        size={260}
      >
        {navigation}
      </Drawer>
      <main className="main-content">
        <Breadcrumb items={[{ title: "工作空间" }, { title: pages[page] }]} />
        <div className="page-heading">
          <div>
            <h1>{page === "home" ? "欢迎来到 Olympus" : pages[page]}</h1>
            <p>
              {page === "home"
                ? "把想法交给协作流程，把关键决定留在你手中。"
                : page === "projects"
                  ? "所有项目，从一份清晰的需求开始。"
                  : page === "workstation"
                    ? "需求、产物与协作进展，在同一处查看。"
                    : page === "agents"
                      ? "明确职责，独立复核，让协作有据可依。"
                      : "开始之前，了解你的工作空间。"}
            </p>
          </div>
          <Button icon={<Plus size={16} />} onClick={newProject}>
            新建项目
          </Button>
        </div>
        {initial.error && (
          <div role="alert" className="storage-alert">
            {initial.error}
          </div>
        )}

        {page === "home" && (
          <>
            <section className="welcome-banner">
              <div>
                <span className="eyebrow">
                  <Sparkles size={14} /> IDEA TO WORKFLOW
                </span>
                <h2>从一个想法，到清晰的下一步。</h2>
                <p>
                  输入具体需求，开启属于你的软件项目。
                  <br />
                  需求、方案与任务将在同一个工作空间中衔接。
                </p>
                <a href="#guide" onClick={() => navigate("guide")}>
                  了解协作流程 <ArrowRight size={15} />
                </a>
              </div>
              <div className="banner-art" aria-hidden="true">
                <div className="art-orbit" />
                <div className="art-card art-input">
                  <MessageSquare size={18} />
                  <span>你的想法</span>
                  <i />
                </div>
                <div className="art-core">
                  <Layers3 size={32} />
                </div>
                <div className="art-card art-output">
                  <Workflow size={18} />
                  <span>协作蓝图</span>
                  <Check size={14} />
                </div>
                <span className="art-dot dot-one" />
                <span className="art-dot dot-two" />
              </div>
            </section>
            <section className="stat-grid" aria-label="项目概况">
              {[
                {
                  title: "项目草稿",
                  value: drafts.length,
                  detail: "已录入的想法",
                  icon: FolderOpen,
                  tone: "blue",
                },
                {
                  title: "生成产物",
                  value: 0,
                  detail: "等待生成服务接入",
                  icon: FileText,
                  tone: "purple",
                },
                {
                  title: "待确认事项",
                  value: 0,
                  detail: "尚未发起评审",
                  icon: ShieldCheck,
                  tone: "teal",
                },
              ].map(({ title, value, detail, icon: Icon, tone }) => (
                <Card key={title} className="stat-card">
                  <span className={`stat-icon ${tone}`}>
                    <Icon size={22} />
                  </span>
                  <div>
                    <span>{title}</span>
                    <strong>
                      {value}
                      <small>项</small>
                    </strong>
                    <small>{detail}</small>
                  </div>
                </Card>
              ))}
            </section>
            <div className="home-grid">
              <Card
                className="prompt-card"
                title={
                  <span className="card-title">
                    <span className="title-icon">
                      <Pencil size={18} />
                    </span>
                    创建你的项目
                  </span>
                }
                extra={
                  <Tag variant="filled" color="blue">
                    开放式需求
                  </Tag>
                }
              >
                <p className="card-intro">
                  不必先有完整方案。告诉我们，你想为谁解决什么问题。
                </p>
                <Form
                  form={form}
                  name="new"
                  layout="vertical"
                  onFinish={submit}
                  requiredMark="optional"
                >
                  {draftFields}
                  <div className="form-footer">
                    <span>
                      <ShieldCheck size={14} />
                      {draftStorageLabel}
                    </span>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<ArrowRight size={16} />}
                      iconPlacement="end"
                    >
                      创建需求草稿
                    </Button>
                  </div>
                </Form>
              </Card>
              <div className="right-rail">
                <Card title="接下来会发生什么" className="journey-card">
                  <Steps
                    orientation="vertical"
                    current={0}
                    items={[
                      { title: "描述需求", content: "说清目标、用户与约束" },
                      { title: "多体讨论", content: "从不同视角澄清与评审" },
                      { title: "确认规格", content: "由你决定范围与取舍" },
                      {
                        title: "设计与任务编排",
                        content: "形成可追踪的任务与依赖",
                      },
                    ]}
                  />
                  <div className="service-note">
                    <Clock3 size={16} />
                    <p>
                      当前可录入和管理需求草稿。
                      <br />
                      自动生成服务将在后续接入。
                    </p>
                  </div>
                </Card>
                <Card className="tip-card">
                  <span className="tip-icon">
                    <CircleHelp size={20} />
                  </span>
                  <h3>让需求更容易被理解</h3>
                  <p>
                    目标用户是谁？最重要的操作是什么？什么必须做，什么可以暂缓？
                  </p>
                  <Button type="link" onClick={() => navigate("guide")}>
                    查看输入指南 <ArrowRight size={14} />
                  </Button>
                </Card>
              </div>
            </div>
            <Card
              className="recent-card"
              title="最近项目"
              extra={
                <Button
                  type="link"
                  onClick={() => {
                    setSearch("");
                    navigate("projects");
                  }}
                >
                  全部项目 <ArrowRight size={14} />
                </Button>
              }
            >
              {projectTable(3)}
            </Card>
          </>
        )}

        {page === "projects" && (
          <Card
            className="project-list"
            title={
              <Space>
                我的项目 <Tag>{drafts.length}</Tag>
              </Space>
            }
            extra={
              <Input
                aria-label="搜索项目"
                placeholder="搜索名称或需求"
                prefix={<Search size={16} />}
                allowClear
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            }
          >
            {projectTable()}
          </Card>
        )}

        {page === "workstation" &&
          (current ? (
            <>
              <Card className="project-summary">
                <div className="project-summary-main">
                  <span className="large-project-icon">
                    <FolderOpen size={26} />
                  </span>
                  <div>
                    <div className="project-summary-title">
                      <h2>{current.name}</h2>
                      <Tag color="blue">需求草稿</Tag>
                    </div>
                    <p>
                      草稿 v{current.revision} <span>·</span> 更新于{" "}
                      {dateText(current.updatedAt)}
                    </p>
                  </div>
                </div>
                <Select
                  aria-label="切换项目"
                  value={current.id}
                  onChange={(id) => {
                    setSelectedId(id);
                    setTab("input");
                  }}
                  options={drafts.map((draft) => ({
                    value: draft.id,
                    label: draft.name,
                  }))}
                />
              </Card>
              <Card
                className="workflow-card"
                title={
                  <span className="card-title">
                    <Workflow size={18} />
                    流程预览
                  </span>
                }
                extra={<Badge status="default" text="尚未执行" />}
              >
                <FlowPreview captured />
              </Card>
              <Card className="workspace-body">
                <Tabs
                  activeKey={tab}
                  onChange={setTab}
                  items={[
                    {
                      key: "input",
                      label: "原始需求",
                      children: (
                        <>
                          <div className="section-toolbar">
                            <div>
                              <h3>需求输入</h3>
                              <p>保留原始描述，作为后续分析的起点。</p>
                            </div>
                            <Space>
                              <Button
                                icon={<Download size={15} />}
                                onClick={() => exportDraft(current)}
                              >
                                导出
                              </Button>
                              <Button
                                icon={<Pencil size={15} />}
                                onClick={() => {
                                  setEditing(current);
                                  editForm.setFieldsValue(current);
                                }}
                              >
                                编辑需求
                              </Button>
                            </Space>
                          </div>
                          <div className="prompt-content">{current.prompt}</div>
                          {current.constraints && (
                            <div className="constraint-block">
                              <h4>约束与补充</h4>
                              <p>{current.constraints}</p>
                            </div>
                          )}
                          <div className="draft-note">
                            <ShieldCheck size={15} />
                            此内容为需求草稿，尚未经过讨论或真人确认。
                          </div>
                        </>
                      ),
                    },
                    {
                      key: "spec",
                      label: "需求规格",
                      children: (
                        <EmptyPanel
                          title="需求规格尚未生成"
                          description="完成需求讨论和方案评价后，在这里查看规格草稿与确认记录。"
                          icon="file"
                        />
                      ),
                    },
                    {
                      key: "design",
                      label: "设计与编排",
                      children: (
                        <EmptyPanel
                          title="还没有任务编排"
                          description="待设计规格与规划服务接入后，在这里查看任务、依赖和执行状态。"
                          icon="flow"
                        />
                      ),
                    },
                    {
                      key: "activity",
                      label: "活动记录",
                      children: (
                        <div className="activity-item">
                          <span className="activity-dot" />
                          <div>
                            <strong>
                              {current.revision === 1
                                ? "已创建需求草稿"
                                : `需求草稿已更新至 v${current.revision}`}
                            </strong>
                            <p>{dateText(current.updatedAt)} · 项目成员</p>
                            <span>
                              此处仅记录当前草稿信息，尚无 Agent 运行事件。
                            </span>
                          </div>
                        </div>
                      ),
                    },
                  ]}
                />
              </Card>
            </>
          ) : (
            <Card>
              <EmptyPanel
                title="选择一个项目，开始协作"
                description="先创建需求草稿，再在工作台查看后续产物与进展。"
                icon="flow"
              />
              <div className="empty-action">
                <Button type="primary" onClick={newProject}>
                  创建第一个项目
                </Button>
              </div>
            </Card>
          ))}

        {page === "agents" && (
          <>
            <div className="roles-intro">
              <ShieldCheck size={21} />
              <span>
                角色职责预览：当前没有 Agent 在线运行。流程由 Harness
                控制，关键决定由真人确认。
              </span>
            </div>
            <div className="role-grid">
              {roles.map(({ name, title, icon: Icon, color, text }) => (
                <Card className="role-card" key={name}>
                  <div className="role-top">
                    <Avatar
                      size={46}
                      shape="square"
                      className={`role-avatar ${color}`}
                      icon={<Icon size={23} />}
                    />
                    <Tag>待接入</Tag>
                  </div>
                  <h2>{name}</h2>
                  <span className="role-title">{title}</span>
                  <p>{text}</p>
                </Card>
              ))}
            </div>
          </>
        )}
        {page === "guide" && (
          <div className="guide-grid">
            <Card title="从需求到协作">
              <FlowPreview />
              <Collapse defaultActiveKey={["1", "2"]} items={guideItems} />
            </Card>
            <Card title="一个清晰的输入">
              <div className="guide-points">
                {[
                  "目标 · 想解决什么问题",
                  "用户 · 谁会使用它",
                  "场景 · 最重要的核心操作",
                  "约束 · 时间、平台与边界",
                ].map((text, index) => (
                  <div key={text}>
                    <span>{index + 1}</span>
                    {text}
                  </div>
                ))}
              </div>
              <Button type="primary" block onClick={newProject}>
                开始描述需求 <ArrowRight size={15} />
              </Button>
            </Card>
          </div>
        )}
        <footer className="page-footer">
          <span>SWE-Olympus · 让协作有迹可循</span>
          <span>需求输入 → 多体讨论 → 规格确认 → 任务编排</span>
        </footer>
      </main>
      <Modal
        title="编辑需求草稿"
        open={Boolean(editing)}
        onCancel={() => setEditing(undefined)}
        onOk={() => editForm.submit()}
        okText="保存修改"
        cancelText="取消"
        width={620}
        forceRender
      >
        <Form
          form={editForm}
          name="edit"
          layout="vertical"
          requiredMark="optional"
          onFinish={(input) => {
            if (!editing) return;
            const updated = createDraft(input, editing);
            if (
              persist(
                drafts.map((draft) =>
                  draft.id === updated.id ? updated : draft,
                ),
              )
            ) {
              setEditing(undefined);
              void message.success("需求已更新，仍为未确认草稿");
            }
          }}
        >
          {draftFields}
        </Form>
      </Modal>
      <Modal
        title="关于这个工作空间"
        open={help}
        onCancel={() => setHelp(false)}
        footer={
          <Button type="primary" onClick={() => setHelp(false)}>
            知道了
          </Button>
        }
      >
        <p className="help-intro">
          Olympus 将软件需求、方案与任务连接在一个可追溯的协作流程中。
        </p>
        <Descriptions
          column={1}
          items={[
            {
              key: "mode",
              label: "当前能力",
              children: "需求录入、草稿管理、导出与流程预览",
            },
            { key: "storage", label: "保存位置", children: draftStorageLabel },
            {
              key: "generation",
              label: "生成服务",
              children: "尚未接入，暂无自动生成产物",
            },
          ]}
        />
      </Modal>
    </div>
  );
}

function EmptyPanel({
  title,
  description,
  icon,
}: {
  title: string;
  description: string;
  icon: "file" | "flow";
}) {
  return (
    <div className="empty-panel">
      <span>
        {icon === "file" ? <ClipboardList size={32} /> : <Workflow size={32} />}
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      <Tag>等待后续服务接入</Tag>
    </div>
  );
}
