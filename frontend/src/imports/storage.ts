import type { ImportedRequirement } from "./model";

export const storageLabel = "仅当前页面会话，刷新后清除";
export function loadImports(): {
  imports: ImportedRequirement[];
  error?: string;
  hasLegacy?: boolean;
} {
  return { imports: [] };
}
export function saveImports(_items: ImportedRequirement[]): void {}
export function exportLegacy(): string | null {
  return null;
}
