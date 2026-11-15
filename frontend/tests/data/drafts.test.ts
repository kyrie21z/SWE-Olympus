import { describe, it, expect } from "vitest";
import { createDraft } from "../../src/projects/model";
import { createDraftStore, DRAFT_KEY } from "../../src/projects/storage";

function memory() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
}
describe("open-ended prompt drafts", () => {
  it("preserves raw input and separate IDs across persistence and revision", () => {
    const storage = memory();
    const store = createDraftStore(() => storage);
    expect(store.load().drafts).toEqual([]);
    const first = createDraft({
      prompt: "  任意领域的需求\n保留原文  ",
      constraints: "八周",
    });
    const second = createDraft({ prompt: "另一个完全不同的项目" });
    const updated = createDraft(
      {
        name: first.name,
        prompt: first.prompt + "\n新边界",
        constraints: first.constraints,
      },
      first,
    );
    store.save([updated, second]);
    const restored = createDraftStore(() => storage).load().drafts;
    expect(restored[0]).toEqual(updated);
    expect(restored[0].id).toBe(first.id);
    expect(restored[0].revision).toBe(2);
    expect(restored[1].id).not.toBe(first.id);
    expect(restored[0]).not.toHaveProperty("confirmation");
  });
  it.each([
    "{broken",
    '{"schemaVersion":99,"drafts":[]}',
    '{"schemaVersion":1,"drafts":[{"name":"伪造"}]}',
  ])("preserves unreadable storage instead of overwriting: %s", (raw) => {
    const storage = memory();
    storage.setItem(DRAFT_KEY, raw);
    const store = createDraftStore(() => storage);
    expect(store.load().error).toBeTruthy();
    expect(() => store.save([])).toThrow();
    expect(storage.getItem(DRAFT_KEY)).toBe(raw);
  });
  it("rejects stale saves and duplicate IDs", () => {
    const storage = memory();
    const a = createDraftStore(() => storage);
    const b = createDraftStore(() => storage);
    a.load();
    b.load();
    const draft = createDraft({ prompt: "一个项目" });
    a.save([draft]);
    expect(() => b.save([])).toThrow("其他页面更新");
    expect(() => a.save([draft, draft])).toThrow("草稿格式异常");
  });
  it("reports storage denial and does not acknowledge a quota failure", () => {
    expect(
      createDraftStore(() => {
        throw new Error("denied");
      }).load().error,
    ).toBeTruthy();
    const store = createDraftStore(() => ({
      getItem: () => null,
      setItem: () => {
        throw new Error("quota");
      },
    }));
    store.load();
    expect(() => store.save([createDraft({ prompt: "任意需求" })])).toThrow(
      "本地保存失败",
    );
  });
  it("rejects blank or oversized prompts", () => {
    expect(() => createDraft({ prompt: "  \n  " })).toThrow();
    expect(() => createDraft({ prompt: "a".repeat(12001) })).toThrow();
  });
});
