import { validateSource, type ImportedRequirement } from "./model";

export const IMPORT_KEY = "olympus.requirement-imports.v1";
export const LEGACY_KEY = "olympus.prompt-drafts.v1";
export const storageLabel = "保存在当前浏览器，不上传";
type StoragePort = Pick<Storage, "getItem" | "setItem">;
const fields = [
  "id",
  "name",
  "fileName",
  "format",
  "size",
  "sourceText",
  "note",
  "importedAt",
];

function isRecord(value: unknown): value is ImportedRequirement {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  if (
    Object.keys(item).length !== fields.length ||
    !fields.every((key) => Object.hasOwn(item, key))
  )
    return false;
  if (!(
    typeof item.id === "string" &&
    /^[0-9a-f-]{36}$/i.test(item.id) &&
    typeof item.name === "string" &&
    item.name.trim().length > 0 &&
    item.name.length <= 80 &&
    typeof item.fileName === "string" &&
    typeof item.sourceText === "string" &&
    typeof item.note === "string" &&
    item.note.length <= 2000 &&
    typeof item.importedAt === "string" &&
    Number.isFinite(Date.parse(item.importedAt)) &&
    item.size === new TextEncoder().encode(item.sourceText).byteLength
  ))
    return false;
  try {
    return validateSource(item.fileName, item.sourceText) === item.format;
  } catch {
    return false;
  }
}
function validateRecords(
  items: unknown,
): asserts items is ImportedRequirement[] {
  if (
    !Array.isArray(items) ||
    items.length > 20 ||
    !items.every(isRecord) ||
    new Set(items.map((item) => item.id)).size !== items.length ||
    items.reduce((sum, item) => sum + item.size, 0) > 2 * 1024 * 1024
  ) {
    throw new Error(
      "导入记录格式异常或超出容量：最多 20 份，总原文不超过 2 MB。现有记录未被覆盖。",
    );
  }
}

export function createImportStore(getStorage: () => StoragePort) {
  let expectedRaw: string | null | undefined;
  let readable = false;
  return {
    load(): {
      imports: ImportedRequirement[];
      error?: string;
      hasLegacy?: boolean;
    } {
      readable = false;
      try {
        const storage = getStorage();
        const raw = storage.getItem(IMPORT_KEY);
        let imports: ImportedRequirement[] = [];
        if (raw !== null) {
          const envelope = JSON.parse(raw);
          if (
            !envelope ||
            envelope.schemaVersion !== 1 ||
            Object.keys(envelope).sort().join() !== "imports,schemaVersion"
          )
            throw new Error("Unsupported import data");
          validateRecords(envelope.imports);
          imports = envelope.imports;
        }
        expectedRaw = raw;
        readable = true;
        return { imports, hasLegacy: storage.getItem(LEGACY_KEY) !== null };
      } catch {
        readable = false;
        return {
          imports: [],
          error:
            "本地导入记录无法读取，原数据已保留。请检查浏览器存储后刷新；不要清除未备份内容。",
        };
      }
    },
    save(imports: ImportedRequirement[]) {
      if (!readable) throw new Error("本地记录尚未成功读取，无法覆盖保存。");
      validateRecords(imports);
      const storage = getStorage();
      if (storage.getItem(IMPORT_KEY) !== expectedRaw)
        throw new Error("导入记录已在其他页面更新，请刷新后重新选择文件。");
      const raw = JSON.stringify({ schemaVersion: 1, imports });
      try {
        storage.setItem(IMPORT_KEY, raw);
      } catch {
        throw new Error(
          "本地保存失败，可能空间不足或存储被禁用。原文件未修改，请保留原文件。",
        );
      }
      expectedRaw = raw;
    },
    legacy: () => getStorage().getItem(LEGACY_KEY),
  };
}
const store = createImportStore(() => window.localStorage);
export const loadImports = () => store.load();
export const saveImports = (items: ImportedRequirement[]) => store.save(items);
export const exportLegacy = () => store.legacy();
