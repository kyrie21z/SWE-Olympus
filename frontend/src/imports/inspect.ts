import {
  validateSource,
  type ImportedRequirement,
  type Inspection,
} from "./model";

// Issue #1 owns the UI seam. Issue #2 supplies the canonical contract precheck.
export const exampleUrl: string | undefined = undefined;
export async function inspectRequirement(
  file: ImportedRequirement,
): Promise<Inspection> {
  try {
    validateSource(file.fileName, file.sourceText);
    if (file.format === "json")
      JSON.parse(file.sourceText.replace(/^\uFEFF/, ""));
  } catch (error) {
    return {
      status: "invalid",
      canSave: false,
      checks: [
        {
          title: "文件内容",
          state: "failed",
          detail: error instanceof Error ? error.message : "文件无法解析。",
        },
      ],
    };
  }
  const material = file.format !== "json";
  return {
    status: material ? "material" : "preview_only",
    canSave: true,
    checks: [
      {
        title: "文件读取",
        state: "passed",
        detail: "可在本地预览，原文保持不变。",
      },
      {
        title: material ? "材料整理" : "公共契约校验",
        state: "pending",
        detail: material
          ? "文本材料尚未转换为结构化需求，也不具有确认效力。"
          : "基础界面暂只检查 JSON 语法；公共契约与指纹检查由数据模块接入。",
      },
      {
        title: "正式接纳",
        state: "pending",
        detail: "来源、授权与依赖有效性由服务端核验，当前不能进入设计生成。",
      },
    ],
  };
}
