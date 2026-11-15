import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { OlympusApp } from "./App";
import "antd/dist/reset.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2468f2",
          colorText: "#202b40",
          colorTextSecondary: "#69758b",
          colorBorder: "#dfe5ef",
          colorBgLayout: "#f5f7fb",
          borderRadius: 8,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
          controlHeight: 38,
        },
      }}
    >
      <AntApp>
        <OlympusApp />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
