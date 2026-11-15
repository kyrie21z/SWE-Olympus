import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import schema from "../../../docs/contracts/schema.json";
import type { Requirement } from "../contracts/generated";
import {
  validateSource,
  type ImportedRequirement,
  type Inspection,
  type CheckItem,
} from "./model";

export const exampleUrl: string | undefined = "/examples/requirement.json";
const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
ajv.addSchema(schema, "olympus-import");
const validate = ajv.compile({ $ref: "olympus-import#/$defs/Requirement" });

// REQ content has fixed ASCII keys, string leaves and arrays. This is a preview
// comparison with Python's fingerprint, not a general JSON hash generator.
function canonicalRequirement(content: Requirement["content"]): string {
  return JSON.stringify({
    out_of_scope: content.out_of_scope,
    requirements: content.requirements.map((item) => ({
      acceptance: item.acceptance,
      id: item.id,
      text: item.text,
    })),
  });
}
async function contentHash(content: Requirement["content"]): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalRequirement(content));
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (b) => b.toString(16).padStart(2, "0"),
  ).join("");
}

export async function inspectRequirement(
  file: ImportedRequirement,
): Promise<Inspection> {
  const checks: CheckItem[] = [];
  const reject = (title: string, detail: string): Inspection => ({
    status: "invalid",
    canSave: false,
    checks: [...checks, { title, state: "failed", detail }],
  });
  let format;
  try {
    format = validateSource(file.fileName, file.sourceText);
  } catch (error) {
    return reject(
      "文件读取",
      error instanceof Error ? error.message : "无法读取文件。",
    );
  }
  checks.push({
    title: "文件读取",
    state: "passed",
    detail: "UTF-8 文件已读取，原文保持不变。",
  });
  if (format !== "json")
    return {
      status: "material",
      canSave: true,
      checks: [
        ...checks,
        {
          title: "材料整理",
          state: "pending",
          detail: "文本材料尚未转换为 REQ，也不具有确认效力。",
        },
        {
          title: "正式接纳",
          state: "pending",
          detail: "需在上游整理需求、补齐来源与确认，再导出 REQ JSON。",
        },
      ],
    };
  let raw: unknown;
  try {
    raw = JSON.parse(file.sourceText.replace(/^\uFEFF/, ""));
  } catch {
    return reject("JSON 语法", "文件不是有效 JSON，请检查后重新导入。");
  }
  if (raw && typeof raw === "object" && "kind" in raw && raw.kind === "DS")
    return reject(
      "输入类型",
      "当前入口接收需求规格 REQ，不能将设计规格 DS 作为需求导入。",
    );
  if (!validate(raw)) {
    const details = (validate.errors ?? [])
      .slice(0, 5)
      .map((error) => `${error.instancePath || "/"} ${error.message}`)
      .join("；");
    return reject(
      "REQ 公共契约",
      `字段、类型或 schema_version 不符合公共契约 1.0。${details}`,
    );
  }
  const req = raw as Requirement;
  checks.push({
    title: "REQ 公共契约",
    state: "passed",
    detail: "与后端生成的 Requirement JSON Schema 一致。",
  });
  if (
    new Set(req.content.requirements.map((item) => item.id)).size !==
    req.content.requirements.length
  )
    return reject("需求编号", "存在重复的需求编号，无法建立明确追踪关系。");
  checks.push({
    title: "需求编号",
    state: "passed",
    detail: `${req.content.requirements.length} 条需求，编号无重复。`,
  });
  if ((await contentHash(req.content)) !== req.content_hash)
    return reject(
      "内容指纹",
      "正文与 content_hash 不一致，请从上游重新导出；不要在本页面重签确认。",
    );
  checks.push({
    title: "内容指纹",
    state: "passed",
    detail: "REQ 正文的本地计算结果与文件携带指纹一致。",
  });
  const confirmation = req.confirmation;
  if (
    confirmation &&
    (confirmation.target.object_id !== req.object_id ||
      confirmation.target.version !== req.version ||
      confirmation.target.content_hash !== req.content_hash)
  )
    return reject(
      "确认目标",
      "确认记录绑定了不同的需求、版本或指纹，不能沿用旧确认。",
    );
  const synthetic =
    req.provenance.some((source) => source.kind === "synthetic_fixture") ||
    confirmation?.origin === "synthetic_fixture";
  const status = synthetic
    ? "synthetic"
    : confirmation
      ? "awaiting_server"
      : "needs_confirmation";
  checks.push(
    confirmation
      ? {
          title: "确认目标",
          state: "passed",
          detail:
            "记录指向当前需求版本；仅比较引用，不证明确认人的身份或权限。",
        }
      : {
          title: "确认记录",
          state: "pending",
          detail: "文件未提供确认记录，需回上游确认后再正式接纳。",
        },
  );
  if (synthetic)
    checks.push({
      title: "构造来源",
      state: "info",
      detail: "这是演示/测试样例，不能作为真实用户授权。",
    });
  checks.push({
    title: "正式接纳",
    state: "pending",
    detail: "来源、确认授权及依赖有效性需服务端核验，当前不发起设计生成。",
  });
  return {
    status,
    canSave: true,
    checks,
    requirement: {
      id: req.object_id,
      version: req.version,
      hash: req.content_hash,
      items: req.content.requirements,
      outOfScope: req.content.out_of_scope,
      sources: req.provenance.map((source) => ({
        kind: source.kind,
        source: source.source_id,
        locator: source.locator,
      })),
      confirmation: confirmation
        ? {
            id: confirmation.confirmation_id,
            actor: confirmation.actor,
            origin: confirmation.origin,
            time: confirmation.confirmed_at,
          }
        : null,
    },
  };
}
