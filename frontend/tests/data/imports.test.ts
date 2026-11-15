import { describe, expect, it } from "vitest";
import example from "../../public/examples/requirement.json";
import { inspectRequirement } from "../../src/imports/inspect";
import {
  readRequirementFile,
  type ImportedRequirement,
} from "../../src/imports/model";
import {
  createImportStore,
  IMPORT_KEY,
  LEGACY_KEY,
} from "../../src/imports/storage";

function record(value: unknown = example): ImportedRequirement {
  const sourceText = JSON.stringify(value);
  return {
    id: crypto.randomUUID(),
    name: "任意业务需求",
    fileName: "requirements.json",
    format: "json",
    size: new TextEncoder().encode(sourceText).length,
    sourceText,
    note: "",
    importedAt: new Date().toISOString(),
  };
}
function memory() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
}

describe("REQ handoff precheck", () => {
  it("matches the Python-generated hash while preserving synthetic status", async () => {
    const report = await inspectRequirement(record());
    expect(report.status).toBe("synthetic");
    expect(report.canSave).toBe(true);
    expect(report.requirement?.hash).toBe(example.content_hash);
    expect(
      report.checks.find((check) => check.title === "正式接纳")?.state,
    ).toBe("pending");
  });
  it("does not authenticate a human claim or accept a missing confirmation", async () => {
    const value = structuredClone(example);
    value.provenance[0].kind = "material";
    value.confirmation.origin = "human";
    expect((await inspectRequirement(record(value))).status).toBe(
      "awaiting_server",
    );
    expect(
      (await inspectRequirement(record({ ...value, confirmation: null })))
        .status,
    ).toBe("needs_confirmation");
  });
  it.each([
    [
      "schema version",
      (value: typeof example) => {
        value.schema_version = "0.9";
      },
    ],
    [
      "changed meaning",
      (value: typeof example) => {
        value.content.requirements[0].text = "新增支付功能";
      },
    ],
    [
      "duplicate IDs",
      (value: typeof example) => {
        value.content.requirements[1].id = value.content.requirements[0].id;
      },
    ],
    [
      "stale confirmation",
      (value: typeof example) => {
        value.confirmation.target.version = 2;
      },
    ],
    [
      "wrong object",
      (value: typeof example) => {
        value.kind = "DS";
      },
    ],
  ])("blocks %s without silently repairing input", async (_, mutate) => {
    const value = structuredClone(example);
    mutate(value);
    const item = record(value);
    const raw = item.sourceText;
    const report = await inspectRequirement(item);
    expect(report.canSave).toBe(false);
    expect(report.status).toBe("invalid");
    expect(item.sourceText).toBe(raw);
  });
  it("keeps Markdown as unconfirmed raw material, including HTML text", async () => {
    const text = "# 原始需求\n<script>alert('text')</script>";
    const file = await readRequirementFile(new File([text], "notes.md"));
    expect(file.sourceText).toBe(text);
    expect((await inspectRequirement(file)).status).toBe("material");
  });
  it("rejects malformed JSON, empty, oversized, binary and unsupported files", async () => {
    expect(
      (await inspectRequirement({ ...record(), sourceText: "{broken" }))
        .canSave,
    ).toBe(false);
    for (const file of [
      new File([""], "empty.txt"),
      new File(["a".repeat(524289)], "large.txt"),
      new File([new Uint8Array([255, 254])], "bad.txt"),
      new File(["a\u0000b"], "binary.txt"),
      new File(["%PDF"], "spec.pdf"),
    ]) {
      await expect(readRequirementFile(file)).rejects.toThrow();
    }
  });
});

describe("local imported-source cache", () => {
  it("roundtrips raw files without persisting approval or deleting legacy prompts", () => {
    const storage = memory();
    storage.setItem(LEGACY_KEY, "legacy-original");
    const store = createImportStore(() => storage);
    expect(store.load().hasLegacy).toBe(true);
    const item = record();
    store.save([item]);
    expect(createImportStore(() => storage).load().imports).toEqual([item]);
    expect(storage.getItem(LEGACY_KEY)).toBe("legacy-original");
    expect(
      JSON.parse(storage.getItem(IMPORT_KEY)!).imports[0],
    ).not.toHaveProperty("report");
  });
  it("preserves corrupted data and rejects stale writes", () => {
    const storage = memory();
    storage.setItem(IMPORT_KEY, "{broken");
    const store = createImportStore(() => storage);
    expect(store.load().error).toBeTruthy();
    expect(() => store.save([])).toThrow();
    expect(storage.getItem(IMPORT_KEY)).toBe("{broken");
    const empty = memory();
    const a = createImportStore(() => empty);
    const b = createImportStore(() => empty);
    a.load();
    b.load();
    a.save([record()]);
    expect(() => b.save([])).toThrow("其他页面更新");
  });
  it("rejects tampered metadata, duplicate records, limits and quota failures", () => {
    const storage = memory();
    const store = createImportStore(() => storage);
    store.load();
    const item = record();
    expect(() => store.save([{ ...item, size: 0 }])).toThrow();
    expect(() => store.save([item, item])).toThrow();
    expect(() =>
      store.save(Array.from({ length: 21 }, () => record())),
    ).toThrow();
    const denied = createImportStore(() => ({
      getItem: () => null,
      setItem: () => {
        throw new Error("quota");
      },
    }));
    denied.load();
    expect(() => denied.save([item])).toThrow("本地保存失败");
  });
});
