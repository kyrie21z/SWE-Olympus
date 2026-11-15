import { useEffect, useRef, useState } from "react";
import {
  Alert,
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
  Input,
  Menu,
  Modal,
  Select,
  Space,
  Spin,
  Steps,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Upload,
} from "antd";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  ChevronRight,
  CircleHelp,
  ClipboardList,
  Clock3,
  Download,
  FileCheck2,
  FileText,
  FolderOpen,
  Github,
  Layers3,
  Menu as MenuIcon,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Workflow,
  X,
} from "lucide-react";
import { navItems, pages, roles, type Page } from "./shell-config";
import {
  downloadText,
  readRequirementFile,
  statusLabels,
  type ImportedRequirement,
  type Inspection,
} from "./imports/model";
import { exampleUrl, inspectRequirement } from "./imports/inspect";
import {
  exportLegacy,
  loadImports,
  saveImports,
  storageLabel,
} from "./imports/storage";
import { InspectionPanel, RequirementDetails } from "./imports/Preview";

const guideItems = [
  {
    key: "1",
    label: "应该导入什么文件？",
    children:
      "正式交接采用上一阶段导出的 REQ JSON，包含需求条目、验收条件、范围外限制、来源、版本、内容指纹和确认记录。业务领域不限。当前支持单文件导入。",
  },
  {
    key: "2",
    label: "只有 Markdown、TXT 或 PDF 怎么办？",
    children:
      "Markdown / TXT 可以作为待整理材料预览，不会自动变成已确认需求。PDF / Word 的解析尚未接入，请先导出 UTF-8 的受支持格式；单文件最多 512 KB。",
  },
  {
    key: "3",
    label: "导入成功后会立即执行吗？",
    children:
      "不会。当前只保存本地导入记录并进行基础检查。来源、确认授权、依赖版本由后续服务端核验，正式接纳后才进入 Athena 设计、Daedalus 任务分解、审查与真人确认，再由 Zeus 编排。",
  },
  {
    key: "4",
    label: "已有设计规格 DS 可以直接执行吗？",
    children:
      "当前主入口接收需求规格 REQ。设计规格 DS 不能冒充 REQ；未来即使接入 DS 导入，也需要关联有效需求，完成计划检查与真人确认。",
  },
  {
    key: "5",
    label: "原文可以修改吗？",
    children: `导入原文只读，补充说明另存。更换规格时建立新的导入记录，不覆盖旧文件。${storageLabel}；可导出原文件备份。`,
  },
];
function getPage(): Page {
  const value = location.hash.slice(1).split("/")[0];
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
      icon: UploadCloud,
      title: "需求导入",
      subtitle: captured ? "本地记录 · 待核验" : "承接上一阶段 REQ",
    },
    { icon: Layers3, title: "设计规格", subtitle: "Athena" },
    {
      icon: ClipboardList,
      title: "任务分解",
      subtitle: "Daedalus · 审查与确认",
    },
    { icon: Workflow, title: "编排执行", subtitle: "Zeus · 受控派发" },
    { icon: ShieldCheck, title: "验收", subtitle: "独立复核 · 真人验收" },
  ];
  return (
    <div className="flow-preview" aria-label="需求导入后的工作流程">
      {stages.map(({ icon: Icon, title, subtitle }, index) => (
        <div className="flow-stage-wrap" key={title}>
          <div className={`flow-stage ${index === 0 ? "flow-active" : ""}`}>
            <Icon size={20} />
            <strong>{title}</strong>
            <span>{subtitle}</span>
          </div>
          {index < stages.length - 1 && (
            <ChevronRight className="flow-arrow" size={17} />
          )}
        </div>
      ))}
    </div>
  );
}

export function OlympusApp() {
  const { message } = App.useApp();
  const [initial] = useState(loadImports);
  const [imports, setImports] = useState(initial.imports);
  const [page, setPage] = useState<Page>(getPage);
  const [selectedId, setSelectedId] = useState<string>();
  const [mobileNav, setMobileNav] = useState(false);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState("requirement");
  const [help, setHelp] = useState(false);
  const [pending, setPending] = useState<ImportedRequirement>();
  const [pendingReport, setPendingReport] = useState<Inspection>();
  const [currentReport, setCurrentReport] = useState<Inspection>();
  const [busy, setBusy] = useState(false);
  const [fileError, setFileError] = useState("");
  const requestId = useRef(0);
  const current = imports.find((item) => item.id === selectedId) ?? imports[0];

  useEffect(() => {
    const change = () => setPage(getPage());
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    document.title = `${pages[page]} · SWE-Olympus`;
    window.scrollTo(0, 0);
  }, [page]);
  useEffect(() => {
    let active = true;
    setCurrentReport(undefined);
    if (current)
      void inspectRequirement(current)
        .then((report) => {
          if (active) setCurrentReport(report);
        })
        .catch(() => {
          if (active)
            setCurrentReport({
              status: "invalid",
              canSave: false,
              checks: [
                {
                  title: "基础校验",
                  state: "failed",
                  detail: "校验服务异常，请重新导入。",
                },
              ],
            });
        });
    return () => {
      active = false;
    };
  }, [current]);

  function navigate(next: Page) {
    setPage(next);
    location.hash = next;
    setMobileNav(false);
  }
  function newImport() {
    navigate("home");
    window.setTimeout(
      () =>
        document
          .getElementById("import-card")
          ?.scrollIntoView({ block: "start" }),
      0,
    );
  }
  function openProject(item: ImportedRequirement) {
    setSelectedId(item.id);
    setTab("requirement");
    navigate("workstation");
  }
  function clearFile() {
    requestId.current++;
    setPending(undefined);
    setPendingReport(undefined);
    setFileError("");
    setBusy(false);
  }
  async function selectFile(file: File) {
    const id = ++requestId.current;
    setBusy(true);
    setFileError("");
    setPending(undefined);
    setPendingReport(undefined);
    try {
      const result = await readRequirementFile(file);
      const report = await inspectRequirement(result);
      if (requestId.current === id) {
        setPending(result);
        setPendingReport(report);
      }
    } catch (error) {
      if (requestId.current === id)
        setFileError(
          error instanceof Error ? error.message : "文件读取失败，请重试。",
        );
    } finally {
      if (requestId.current === id) setBusy(false);
    }
  }
  function saveImport() {
    if (!pending || !pendingReport?.canSave || busy) return;
    const item = {
      ...pending,
      name: pending.name.trim() || pending.fileName.replace(/\.[^.]+$/, ""),
    };
    try {
      const next = [item, ...imports];
      saveImports(next);
      setImports(next);
      clearFile();
      openProject(item);
      void message.success("已保存本地导入记录，尚未进入正式流程");
    } catch (error) {
      void message.error(
        error instanceof Error ? error.message : "本地保存失败，请保留原文件。",
      );
    }
  }
  function legacyDownload() {
    try {
      const raw = exportLegacy();
      if (raw) downloadText("olympus-legacy-prompt-drafts.json", raw);
    } catch {
      void message.error("旧版草稿无法读取，请检查浏览器存储。");
    }
  }
  const columns = [
    {
      title: "项目 / 来源文件",
      key: "name",
      render: (_: unknown, item: ImportedRequirement) => (
        <button className="project-name" onClick={() => openProject(item)}>
          <span className="project-glyph">
            <FileText size={19} />
          </span>
          <span>
            <strong>{item.name}</strong>
            <small>{item.fileName}</small>
          </span>
        </button>
      ),
    },
    {
      title: "导入类型",
      key: "type",
      width: 130,
      render: (_: unknown, item: ImportedRequirement) => (
        <Tag color={item.format === "json" ? "blue" : "default"}>
          {item.format === "json" ? "REQ 待核验" : "待整理材料"}
        </Tag>
      ),
    },
    {
      title: "导入时间",
      key: "time",
      width: 140,
      render: (_: unknown, item: ImportedRequirement) => (
        <span className="muted">{dateText(item.importedAt)}</span>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_: unknown, item: ImportedRequirement) => (
        <Button type="link" onClick={() => openProject(item)}>
          打开 <ArrowUpRight size={14} />
        </Button>
      ),
    },
  ];
  const projectTable = (limit?: number) => (
    <Table
      rowKey="id"
      columns={columns}
      dataSource={imports
        .filter((item) =>
          `${item.name} ${item.fileName}`
            .toLowerCase()
            .includes(search.toLowerCase()),
        )
        .slice(0, limit)}
      scroll={{ x: 600 }}
      pagination={limit ? false : { pageSize: 6, hideOnSinglePage: true }}
      locale={{
        emptyText: (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              imports.length
                ? "没有匹配的导入记录"
                : "暂无导入记录，请选择一份需求规格文件"
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
                ? "承接已确认的需求，让设计、任务与执行有据可循。"
                : page === "projects"
                  ? "查看导入的需求规格与来源材料。"
                  : page === "workstation"
                    ? "从需求交接开始，跟踪设计、编排与验收。"
                    : page === "agents"
                      ? "明确职责，独立复核，让协作有据可依。"
                      : "了解输入文件与正式流程的边界。"}
            </p>
          </div>
          <Button icon={<Plus size={16} />} onClick={newImport}>
            导入需求
          </Button>
        </div>
        {initial.error && (
          <Alert
            className="import-alert"
            type="warning"
            showIcon
            title={initial.error}
          />
        )}
        {initial.hasLegacy && (
          <Alert
            className="import-alert"
            type="info"
            showIcon
            title="旧版 Prompt 草稿已保留，不会自动作为确认版规格导入。"
            action={
              <Button size="small" onClick={legacyDownload}>
                导出旧版草稿
              </Button>
            }
          />
        )}
        {page === "home" && (
          <>
            <section className="welcome-banner">
              <div>
                <span className="eyebrow">
                  <Sparkles size={14} /> REQUIREMENTS TO EXECUTION
                </span>
                <h2>从需求规格，到可追踪的执行。</h2>
                <p>
                  导入上一阶段产物，衔接设计、任务分解与编排。
                  <br />
                  业务内容由文件决定，关键确认始终由人掌握。
                </p>
                <a href="#guide" onClick={() => navigate("guide")}>
                  了解文件交接 <ArrowRight size={15} />
                </a>
              </div>
              <div className="banner-art" aria-hidden="true">
                <div className="art-orbit" />
                <div className="art-card art-input">
                  <FileCheck2 size={18} />
                  <span>需求规格 REQ</span>
                  <i />
                </div>
                <div className="art-core">
                  <Layers3 size={32} />
                </div>
                <div className="art-card art-output">
                  <Workflow size={18} />
                  <span>任务编排</span>
                  <Check size={14} />
                </div>
                <span className="art-dot dot-one" />
                <span className="art-dot dot-two" />
              </div>
            </section>
            <section className="stat-grid" aria-label="项目概况">
              {[
                {
                  title: "导入记录",
                  value: imports.length,
                  detail: "本地保存，尚未正式接纳",
                  icon: FolderOpen,
                  tone: "blue",
                },
                {
                  title: "设计与计划",
                  value: 0,
                  detail: "等待后续生成服务接入",
                  icon: FileText,
                  tone: "purple",
                },
                {
                  title: "执行任务",
                  value: 0,
                  detail: "编排尚未启动",
                  icon: Workflow,
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
                id="import-card"
                className="prompt-card import-card"
                title={
                  <span className="card-title">
                    <span className="title-icon">
                      <UploadCloud size={19} />
                    </span>
                    导入需求规格
                  </span>
                }
                extra={
                  <Tag variant="filled" color="blue">
                    上一阶段产物
                  </Tag>
                }
              >
                <p className="card-intro">
                  选择上一阶段导出的 REQ JSON；文本材料将保持待整理状态。
                </p>
                <Upload.Dragger
                  accept=".json,.md,.txt"
                  multiple={false}
                  maxCount={1}
                  showUploadList={false}
                  beforeUpload={(file) => {
                    void selectFile(file);
                    return false;
                  }}
                  aria-label="选择需求文件"
                >
                  <div className="upload-symbol">
                    <UploadCloud size={34} />
                  </div>
                  <strong>点击选择或拖拽文件到此处</strong>
                  <p>JSON 需求规格 · Markdown / TXT 材料</p>
                  <small>UTF-8 · 单文件 ≤ 512 KB · 仅在本地读取</small>
                </Upload.Dragger>
                {exampleUrl && (
                  <Button
                    className="sample-download"
                    type="link"
                    href={exampleUrl}
                    download="requirement-example.json"
                    icon={<Download size={14} />}
                  >
                    下载交接样例（构造）
                  </Button>
                )}
                <p className="format-note">
                  PDF / Word 解析尚未接入；设计规格 DS 导入留作后续扩展。
                </p>
                {busy && (
                  <div className="import-loading">
                    <Spin size="small" /> 正在读取与检查文件…
                  </div>
                )}
                {fileError && (
                  <Alert
                    className="import-alert"
                    type="error"
                    showIcon
                    title={fileError}
                  />
                )}
                {pending && (
                  <>
                    <div className="selected-file">
                      <FileText size={21} />
                      <div>
                        <strong>{pending.fileName}</strong>
                        <span>
                          {(pending.size / 1024).toFixed(1)} KB ·{" "}
                          {pending.format.toUpperCase()}
                        </span>
                      </div>
                      <Button
                        type="text"
                        aria-label="移除所选文件"
                        icon={<X size={16} />}
                        onClick={clearFile}
                      />
                    </div>
                    <div className="import-fields">
                      <label htmlFor="import-name">
                        项目名称 <small>选填</small>
                      </label>
                      <Input
                        id="import-name"
                        value={pending.name}
                        maxLength={80}
                        onChange={(event) =>
                          setPending({ ...pending, name: event.target.value })
                        }
                      />
                      <label htmlFor="import-note">
                        补充说明 <small>不改写规格正文或确认记录</small>
                      </label>
                      <Input.TextArea
                        id="import-note"
                        value={pending.note}
                        maxLength={2000}
                        autoSize={{ minRows: 2, maxRows: 5 }}
                        placeholder="例如本次交接背景、需要优先关注的约束"
                        onChange={(event) =>
                          setPending({ ...pending, note: event.target.value })
                        }
                      />
                    </div>
                    {pendingReport && (
                      <>
                        <InspectionPanel report={pendingReport} />
                        <Collapse
                          className="import-preview"
                          items={[
                            {
                              key: "preview",
                              label: "查看文件内容与需求摘要",
                              children: (
                                <RequirementDetails
                                  file={pending}
                                  report={pendingReport}
                                />
                              ),
                            },
                          ]}
                        />
                      </>
                    )}
                  </>
                )}
                <div className="form-footer">
                  <span>
                    <ShieldCheck size={14} />
                    {storageLabel}
                  </span>
                  <Button
                    type="primary"
                    disabled={
                      !pendingReport?.canSave || busy || Boolean(initial.error)
                    }
                    onClick={saveImport}
                    icon={<ArrowRight size={16} />}
                    iconPlacement="end"
                  >
                    保存导入并查看
                  </Button>
                </div>
              </Card>
              <div className="right-rail">
                <Card title="文件进入系统之后" className="journey-card">
                  <Steps
                    orientation="vertical"
                    current={0}
                    items={[
                      { title: "需求导入", content: "预览、检查与服务端接纳" },
                      { title: "设计规格", content: "Athena 生成设计" },
                      {
                        title: "任务分解",
                        content: "Daedalus 规划 · 审查与确认",
                      },
                      { title: "编排执行", content: "Zeus 按规则派发" },
                      { title: "验收", content: "独立复核与真人验收" },
                    ]}
                  />
                  <div className="service-note">
                    <Clock3 size={16} />
                    <p>
                      当前为文件交接与预览。
                      <br />
                      正式接纳、生成和执行服务待接入。
                    </p>
                  </div>
                </Card>
                <Card className="tip-card">
                  <span className="tip-icon">
                    <CircleHelp size={20} />
                  </span>
                  <h3>上传不代表已经确认</h3>
                  <p>
                    规格需要携带版本、指纹和确认记录。文件中的声明仍需核实，材料不能直接进入执行。
                  </p>
                  <Button type="link" onClick={() => navigate("guide")}>
                    查看交接说明 <ArrowRight size={14} />
                  </Button>
                </Card>
              </div>
            </div>
            <Card
              className="recent-card"
              title="最近导入"
              extra={
                <Button
                  type="link"
                  onClick={() => {
                    setSearch("");
                    navigate("projects");
                  }}
                >
                  全部记录 <ArrowRight size={14} />
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
                导入记录 <Tag>{imports.length}</Tag>
              </Space>
            }
            extra={
              <Input
                aria-label="搜索项目"
                placeholder="搜索项目或文件名"
                prefix={<Search size={16} />}
                allowClear
                value={search}
                onChange={(event) => setSearch(event.target.value)}
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
                    <FileCheck2 size={26} />
                  </span>
                  <div>
                    <div className="project-summary-title">
                      <h2>{current.name}</h2>
                      <Tag color="gold">
                        {currentReport
                          ? statusLabels[currentReport.status]
                          : "检查中"}
                      </Tag>
                    </div>
                    <p>
                      {current.fileName}
                      <span>·</span>
                      {dateText(current.importedAt)}
                    </p>
                  </div>
                </div>
                <Select
                  aria-label="切换项目"
                  value={current.id}
                  onChange={(id) => {
                    setSelectedId(id);
                    setTab("requirement");
                  }}
                  options={imports.map((item) => ({
                    value: item.id,
                    label: item.name,
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
                      key: "requirement",
                      label: "导入规格",
                      children: (
                        <>
                          <div className="section-toolbar">
                            <div>
                              <h3>需求交接内容</h3>
                              <p>原文件只读，补充说明独立保存。</p>
                            </div>
                            <Space>
                              <Button
                                icon={<Download size={15} />}
                                onClick={() =>
                                  downloadText(
                                    current.fileName,
                                    current.sourceText,
                                  )
                                }
                              >
                                导出原文件
                              </Button>
                              <Button onClick={newImport}>导入新版本</Button>
                            </Space>
                          </div>
                          {currentReport ? (
                            <>
                              <InspectionPanel report={currentReport} />
                              <RequirementDetails
                                file={current}
                                report={currentReport}
                              />
                              <Collapse
                                items={[
                                  {
                                    key: "raw",
                                    label: "查看原始文件",
                                    children: (
                                      <pre className="raw-source">
                                        {current.sourceText}
                                      </pre>
                                    ),
                                  },
                                ]}
                              />
                            </>
                          ) : (
                            <Spin />
                          )}
                          <div className="next-stage">
                            <span>下一步：服务端接纳后进入 Athena 设计。</span>
                            <Button disabled>生成设计规格（待接入）</Button>
                          </div>
                        </>
                      ),
                    },
                    {
                      key: "design",
                      label: "设计规格",
                      children: (
                        <EmptyPanel
                          title="设计规格尚未生成"
                          description="需求经正式接纳后，由 Athena 生成带来源与验收方式的设计规格 DS。"
                        />
                      ),
                    },
                    {
                      key: "plan",
                      label: "任务分解",
                      children: (
                        <EmptyPanel
                          title="任务计划尚未生成"
                          description="Daedalus 根据设计形成任务与依赖，审查和真人确认通过后进入编排。"
                        />
                      ),
                    },
                    {
                      key: "execution",
                      label: "编排执行",
                      children: (
                        <EmptyPanel
                          title="编排器尚未启动"
                          description="这里将接入任务 DAG、Agent 执行记录、阻塞原因与受控操作。"
                        />
                      ),
                    },
                    {
                      key: "acceptance",
                      label: "验收",
                      children: (
                        <EmptyPanel
                          title="暂无验收结果"
                          description="仅依据程序检查、独立复核和真人验收展示结果。"
                        />
                      ),
                    },
                  ]}
                />
              </Card>
            </>
          ) : (
            <Card>
              <EmptyPanel
                title="导入一份规格，建立项目上下文"
                description="请先选择上一阶段的需求文件，再查看设计、任务与编排的承接位置。"
              />
              <div className="empty-action">
                <Button type="primary" onClick={newImport}>
                  导入第一份规格
                </Button>
              </div>
            </Card>
          ))}
        {page === "agents" && (
          <>
            <div className="roles-intro">
              <ShieldCheck size={21} />
              <span>
                职责预览：上一阶段负责需求确认；Zeus 是确定性程序，YOU
                是真人。下列 Agent 尚未接入运行。
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
            <Card title="需求规格交接">
              <FlowPreview />
              <Collapse defaultActiveKey={["1", "3"]} items={guideItems} />
            </Card>
            <Card title="文件中需要保留">
              <div className="guide-points">
                {[
                  "需求条目与验收条件",
                  "范围与范围外限制",
                  "来源、版本与内容指纹",
                  "绑定当前版本的确认记录",
                ].map((text, index) => (
                  <div key={text}>
                    <span>{index + 1}</span>
                    {text}
                  </div>
                ))}
              </div>
              <Button type="primary" block onClick={newImport}>
                选择需求文件 <ArrowRight size={15} />
              </Button>
            </Card>
          </div>
        )}
        <footer className="page-footer">
          <span>SWE-Olympus · 让协作有迹可循</span>
          <span>需求导入 → 设计规格 → 任务分解 → 编排执行 → 验收</span>
        </footer>
      </main>
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
          承接上一阶段的确认版需求，衔接本阶段的设计、任务与可视化编排。
        </p>
        <Descriptions
          column={1}
          items={[
            {
              key: "input",
              label: "当前入口",
              children: "REQ JSON；Markdown / TXT 待整理材料",
            },
            { key: "storage", label: "保存位置", children: storageLabel },
            {
              key: "scope",
              label: "当前能力",
              children: "本地文件读取、预览、基础检查与记录保存",
            },
            {
              key: "next",
              label: "后续接入",
              children: "服务端确认核验、设计生成、任务分解与编排执行",
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
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty-panel">
      <span>
        <Workflow size={32} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      <Tag>等待后续服务接入</Tag>
    </div>
  );
}
