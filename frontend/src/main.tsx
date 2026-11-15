import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Layers3,
  LayoutGrid,
  Workflow,
  ChevronRight,
  ArrowUpRight,
  ShieldCheck,
} from "lucide-react";
import "./styles.css";

function App() {
  const [page, setPage] = useState("projects");
  return (
    <div className="app">
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
          {[
            ["projects", "项目中心"],
            ["workstation", "工作台"],
          ].map(([id, label]) => (
            <button
              key={id}
              className={page === id ? "nav-item active" : "nav-item"}
              onClick={() => setPage(id)}
              title={label}
            >
              {id === "projects" ? (
                <LayoutGrid size={18} />
              ) : (
                <Workflow size={18} />
              )}
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <ShieldCheck size={18} />
          <p>
            人掌握关键决定<span>让过程可见，让结果可核对。</span>
          </p>
        </div>
      </aside>
      <div className="main">
        <header>
          <div className="breadcrumb">
            工作空间 <ChevronRight size={14} />{" "}
            <strong>{page === "projects" ? "项目中心" : "工作台"}</strong>
          </div>
          <span className="tag">P1 · 工程基础</span>
        </header>
        <main>
          <div className="eyebrow">SWE–OLYMPUS / FOUNDATION</div>
          <div className="page-heading">
            <div>
              <h1>
                {page === "projects"
                  ? "每一个想法，都有清晰的下一步。"
                  : "项目工作台"}
              </h1>
              <p>从确认需求到可验证交付，在同一个工作空间组织协作。</p>
            </div>
          </div>
          <div className="notice">
            工程启动壳 · 公共任务数据将在下一项交付中接入。
          </div>
          {page === "projects" ? (
            <article className="project-card" style={{ marginTop: 28 }}>
              <div className="project-top">
                <div className="project-symbol">
                  <Workflow size={25} />
                </div>
                <span className="tag">课程项目</span>
              </div>
              <h2>SWE-Olympus 项目空间</h2>
              <p>
                工程基础、统一命令与检查入口已就绪。进入工作台查看后续模块的承接位置。
              </p>
              <footer>
                <span className="caption">Issue #1 / 工程基础</span>
                <button
                  className="primary"
                  onClick={() => setPage("workstation")}
                >
                  进入工作台 <ArrowUpRight size={16} />
                </button>
              </footer>
            </article>
          ) : (
            <section className="empty-state">
              <Workflow size={34} />
              <h2>工作台基础已就绪</h2>
              <p>下一项交付将接入公共契约、任务样例和只读数据模块。</p>
              <button
                className="text-button"
                onClick={() => setPage("projects")}
              >
                返回项目中心 <ChevronRight size={16} />
              </button>
            </section>
          )}
          <div className="page-footer">
            <span>OLYMPUS · 让协作有迹可循</span>
            <span>基础环境</span>
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
