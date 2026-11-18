import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const example = JSON.parse(
  readFileSync(
    new URL("../../public/examples/requirement.json", import.meta.url),
    "utf8",
  ),
);

test("structured REQ import survives reload and stays outside formal execution", async ({
  page,
}, info) => {
  await page.goto("/");
  const upload = page.locator('input[type="file"]');
  const invalid = structuredClone(example);
  invalid.confirmation.target.version = 9;
  await upload.setInputFiles({
    name: "stale.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(invalid)),
  });
  await expect(page.getByText("校验未通过", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存导入并查看" }),
  ).toBeDisabled();
  await upload.setInputFiles({
    name: "knowledge-REQ.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(example, null, 2)),
  });
  await expect(page.getByText("构造样例", { exact: true })).toBeVisible();
  await page.getByLabel("项目名称").fill("团队文档 · 需求交接示例");
  await page.getByRole("button", { name: "保存导入并查看" }).click();
  await expect(page.getByText("FR-001", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("FR-001", { exact: true })).toBeVisible();
  await expect(
    page.getByText("按标题检索团队文档", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "建立运行并核验需求" }),
  ).toBeDisabled();
  await page.screenshot({
    path: info.outputPath("structured-import.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "编排执行", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "P6 运行控制台已接入" }),
  ).toBeVisible();
});

test("legacy Prompt drafts are retained for export and never reinterpreted as REQ", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem(
      "olympus.prompt-drafts.v1",
      '{"schemaVersion":1,"drafts":[{"prompt":"旧输入"}]}',
    ),
  );
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "导出旧版草稿" }),
  ).toBeVisible();
  await expect(page.locator(".ant-table-row")).toHaveCount(0);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出旧版草稿" }).click();
  expect((await download).suggestedFilename()).toBe(
    "olympus-legacy-prompt-drafts.json",
  );
});
