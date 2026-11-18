import { Alert, Descriptions, Table, Tag } from "antd";
import { CheckCircle2, Circle, FileText, XCircle } from "lucide-react";
import {
  statusLabels,
  type ImportedRequirement,
  type Inspection,
} from "./model";

export function InspectionPanel({ report }: { report: Inspection }) {
  return (
    <div className="inspection-panel">
      <div className="inspection-title">
        <strong>基础校验</strong>
        <Tag color={report.canSave ? "gold" : "error"}>
          {statusLabels[report.status]}
        </Tag>
      </div>
      {report.checks.map((item, index) => (
        <div className={`check-item check-${item.state}`} key={index}>
          {item.state === "passed" ? (
            <CheckCircle2 size={17} />
          ) : item.state === "failed" ? (
            <XCircle size={17} />
          ) : (
            <Circle size={17} />
          )}
          <div>
            <strong>{item.title}</strong>
            <p>{item.detail}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

export function RequirementDetails({
  file,
  report,
}: {
  file: ImportedRequirement;
  report: Inspection;
}) {
  const req = report.requirement;
  return (
    <div className="requirement-details">
      <Alert
        type="info"
        showIcon
        title="本地导入不等于正式接纳"
        description="本区只读预览原文。请通过工作台建立运行并明确接纳需求，再进入设计、规划和执行；每个阶段保留人工批准。"
      />
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2 }}
        items={[
          { key: "file", label: "来源文件", children: file.fileName },
          {
            key: "format",
            label: "文件格式",
            children: file.format.toUpperCase(),
          },
          ...(req
            ? [
                { key: "id", label: "需求编号", children: req.id },
                {
                  key: "version",
                  label: "需求版本",
                  children: `v${req.version}`,
                },
                {
                  key: "hash",
                  label: "内容指纹",
                  children: <code className="hash-text">{req.hash}</code>,
                  span: 2,
                },
              ]
            : []),
        ]}
      />
      {req ? (
        <>
          <h3>需求条目与验收条件</h3>
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            scroll={{ x: 560 }}
            dataSource={req.items}
            columns={[
              { title: "编号", dataIndex: "id", width: 110 },
              { title: "需求内容", dataIndex: "text" },
              { title: "验收条件", dataIndex: "acceptance" },
            ]}
          />
          <h3>范围外限制</h3>
          {req.outOfScope.length ? (
            <ul className="scope-list">
              {req.outOfScope.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">文件声明没有范围外条目，仍需上游核实。</p>
          )}
          <h3>来源记录</h3>
          <div className="source-list">
            {req.sources.map((source, index) => (
              <div key={index}>
                <Tag>{source.kind}</Tag>
                <span>
                  {source.source} · {source.locator}
                </span>
              </div>
            ))}
          </div>
          <h3>文件携带的确认记录</h3>
          {req.confirmation ? (
            <Descriptions
              size="small"
              column={1}
              items={[
                { key: "id", label: "确认编号", children: req.confirmation.id },
                {
                  key: "actor",
                  label: "声明确认人",
                  children: req.confirmation.actor,
                },
                {
                  key: "origin",
                  label: "记录来源",
                  children:
                    req.confirmation.origin === "synthetic_fixture"
                      ? "构造样例，不是真人授权"
                      : "文件声明为 human，尚未核实",
                },
                {
                  key: "time",
                  label: "声明时间",
                  children: req.confirmation.time,
                },
              ]}
            />
          ) : (
            <p className="muted">未提供确认记录。需回到上一阶段完成确认。</p>
          )}
        </>
      ) : (
        <div className="material-preview">
          <FileText size={20} />
          <h3>{file.format === "json" ? "文件原文预览" : "需求材料预览"}</h3>
          <p>原文只读，不自动提取需求、补写事实或生成确认。</p>
          <pre>{file.sourceText}</pre>
        </div>
      )}
      {file.note && (
        <div className="constraint-block">
          <h4>补充说明（不改变规格正文）</h4>
          <p>{file.note}</p>
        </div>
      )}
    </div>
  );
}
