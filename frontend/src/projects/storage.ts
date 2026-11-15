import type { ProjectDraft } from "./model";

// Prompt drafts are user input, never a confirmed Requirement/Design/Run.
export const DRAFT_KEY = "olympus.prompt-drafts.v1";
export const draftStorageLabel = "保存在当前浏览器，不上传";
type StoragePort = Pick<Storage, "getItem" | "setItem">;
const fields = [
  "id",
  "name",
  "prompt",
  "constraints",
  "createdAt",
  "updatedAt",
  "revision",
];

function isDraft(value: unknown): value is ProjectDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const d = value as Record<string, unknown>;
  if (
    Object.keys(d).length !== fields.length ||
    !fields.every((key) => Object.hasOwn(d, key))
  )
    return false;
  return (
    typeof d.id === "string" &&
    /^[0-9a-f-]{36}$/i.test(d.id) &&
    typeof d.name === "string" &&
    d.name.trim().length > 0 &&
    d.name.length <= 80 &&
    typeof d.prompt === "string" &&
    d.prompt.trim().length > 0 &&
    d.prompt.length <= 12000 &&
    typeof d.constraints === "string" &&
    d.constraints.length <= 2000 &&
    typeof d.createdAt === "string" &&
    Number.isFinite(Date.parse(d.createdAt)) &&
    typeof d.updatedAt === "string" &&
    Number.isFinite(Date.parse(d.updatedAt)) &&
    Date.parse(d.updatedAt) >= Date.parse(d.createdAt) &&
    typeof d.revision === "number" &&
    Number.isSafeInteger(d.revision) &&
    d.revision >= 1
  );
}

function validateDrafts(drafts: unknown): asserts drafts is ProjectDraft[] {
  if (
    !Array.isArray(drafts) ||
    drafts.length > 100 ||
    !drafts.every(isDraft) ||
    new Set(drafts.map((d) => d.id)).size !== drafts.length
  ) {
    throw new Error("草稿格式异常或已达到 100 项上限；现有内容未被覆盖。");
  }
}

export function createDraftStore(getStorage: () => StoragePort) {
  let expectedRaw: string | null | undefined;
  let readable = false;
  return {
    load(): { drafts: ProjectDraft[]; error?: string } {
      readable = false;
      try {
        const raw = getStorage().getItem(DRAFT_KEY);
        let drafts: ProjectDraft[] = [];
        if (raw !== null) {
          const envelope = JSON.parse(raw);
          if (
            !envelope ||
            envelope.schemaVersion !== 1 ||
            Object.keys(envelope).sort().join() !== "drafts,schemaVersion"
          ) {
            throw new Error("Unsupported draft data");
          }
          validateDrafts(envelope.drafts);
          drafts = envelope.drafts;
        }
        expectedRaw = raw;
        readable = true;
        return { drafts };
      } catch {
        return {
          drafts: [],
          error:
            "本地草稿无法读取，已保留原数据并暂停保存。请检查浏览器存储后刷新；不要清除尚未备份的数据。",
        };
      }
    },
    save(drafts: ProjectDraft[]) {
      if (!readable) throw new Error("本地草稿尚未成功读取，无法覆盖保存。");
      validateDrafts(drafts);
      const storage = getStorage();
      if (storage.getItem(DRAFT_KEY) !== expectedRaw)
        throw new Error(
          "草稿已在其他页面更新，请先导出当前内容，再刷新后编辑。",
        );
      const raw = JSON.stringify({ schemaVersion: 1, drafts });
      try {
        storage.setItem(DRAFT_KEY, raw);
      } catch {
        throw new Error(
          "本地保存失败，可能空间不足或存储被禁用；请保留当前输入并导出已有草稿。",
        );
      }
      expectedRaw = raw;
    },
  };
}

const store = createDraftStore(() => window.localStorage);
export const loadDrafts = () => store.load();
export const saveDrafts = (drafts: ProjectDraft[]) => store.save(drafts);
