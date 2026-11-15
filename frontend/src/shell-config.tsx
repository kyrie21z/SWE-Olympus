import {
  Home,
  FolderOpen,
  Workflow,
  Bot,
  BookOpen,
  Layers3,
  Waypoints,
  Boxes,
  MessageSquare,
  ShieldCheck,
  GitBranch,
} from "lucide-react";
export type Page = "home" | "projects" | "workstation" | "agents" | "guide";
export const pages = {
  home: "欢迎总览",
  projects: "项目中心",
  workstation: "项目工作台",
  agents: "协作角色",
  guide: "使用指南",
};
export const navItems = [
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
export const roles = [
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
