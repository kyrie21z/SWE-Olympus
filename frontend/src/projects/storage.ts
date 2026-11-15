import type { ProjectDraft } from "./model";

// Issue #1 provides the UI seam; Issue #2 supplies durable local draft storage.
export const draftStorageLabel = "仅当前页面会话，刷新后清除";
export function loadDrafts(): { drafts: ProjectDraft[]; error?: string } {
  return { drafts: [] };
}
export function saveDrafts(_drafts: ProjectDraft[]): void {}
