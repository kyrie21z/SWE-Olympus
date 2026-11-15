export const MAX_FILE_BYTES = 512 * 1024;
export type FileFormat = "json" | "md" | "txt";
export interface ImportedRequirement {
  id: string;
  name: string;
  fileName: string;
  format: FileFormat;
  size: number;
  sourceText: string;
  note: string;
  importedAt: string;
}
export interface CheckItem {
  title: string;
  state: "passed" | "failed" | "pending" | "info";
  detail: string;
}
export interface RequirementPreview {
  id: string;
  version: number;
  hash: string;
  items: { id: string; text: string; acceptance: string }[];
  outOfScope: string[];
  sources: { kind: string; source: string; locator: string }[];
  confirmation: null | {
    id: string;
    actor: string;
    origin: string;
    time: string;
  };
}
export type ImportStatus =
  | "preview_only"
  | "material"
  | "needs_confirmation"
  | "synthetic"
  | "awaiting_server"
  | "invalid";
export interface Inspection {
  status: ImportStatus;
  canSave: boolean;
  checks: CheckItem[];
  requirement?: RequirementPreview;
}
export const statusLabels: Record<ImportStatus, string> = {
  preview_only: "待契约校验",
  material: "待整理材料",
  needs_confirmation: "待补充确认",
  synthetic: "构造样例",
  awaiting_server: "待服务端核验",
  invalid: "校验未通过",
};

export function fileFormat(name: string): FileFormat {
  const ext = name.split(".").pop()?.toLowerCase();
  if (ext !== "json" && ext !== "md" && ext !== "txt") {
    throw new Error(
      "当前支持 JSON、Markdown 和 TXT；PDF / Word 解析尚未接入，请先导出支持的格式。",
    );
  }
  return ext;
}

export function validateSource(name: string, sourceText: string): FileFormat {
  const format = fileFormat(name);
  if (!name.trim() || name.length > 240) throw new Error("文件名过长或为空。");
  if (!sourceText.trim())
    throw new Error("文件为空，请选择包含需求内容的文件。");
  if (new TextEncoder().encode(sourceText).byteLength > MAX_FILE_BYTES)
    throw new Error("单个文件不能超过 512 KB。");
  if (sourceText.includes("\u0000"))
    throw new Error("文件包含二进制内容，请使用 UTF-8 文本文件。");
  return format;
}

export async function readRequirementFile(
  file: File,
): Promise<ImportedRequirement> {
  fileFormat(file.name);
  if (file.size > MAX_FILE_BYTES) throw new Error("单个文件不能超过 512 KB。");
  let sourceText: string;
  try {
    sourceText = new TextDecoder("utf-8", {
      fatal: true,
      ignoreBOM: true,
    }).decode(await file.arrayBuffer());
  } catch {
    throw new Error("无法读取 UTF-8 文本，请检查文件编码后重试。");
  }
  return {
    id: crypto.randomUUID(),
    name: file.name.replace(/\.[^.]+$/, "").slice(0, 80),
    fileName: file.name,
    format: validateSource(file.name, sourceText),
    size: file.size,
    sourceText,
    note: "",
    importedAt: new Date().toISOString(),
  };
}

export function downloadText(fileName: string, text: string) {
  const url = URL.createObjectURL(
    new Blob([text], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
