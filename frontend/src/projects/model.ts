export interface ProjectDraft {
  id: string;
  name: string;
  prompt: string;
  constraints: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface DraftInput {
  name?: string;
  prompt: string;
  constraints?: string;
}

export function createDraft(
  input: DraftInput,
  previous?: ProjectDraft,
): ProjectDraft {
  if (!input.prompt.trim() || input.prompt.length > 12000)
    throw new Error("请输入 1–12000 字的需求描述。");
  if ((input.name?.length ?? 0) > 80 || (input.constraints?.length ?? 0) > 2000)
    throw new Error("项目名称或约束过长。");
  const now = new Date().toISOString();
  return {
    id: previous?.id ?? crypto.randomUUID(),
    name:
      input.name?.trim() || input.prompt.trim().split(/\r?\n/)[0].slice(0, 24),
    prompt: input.prompt,
    constraints: input.constraints ?? "",
    createdAt: previous?.createdAt ?? now,
    updatedAt: now,
    revision: previous ? previous.revision + 1 : 1,
  };
}
