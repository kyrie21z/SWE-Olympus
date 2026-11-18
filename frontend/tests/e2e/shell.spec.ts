import { test, expect } from "@playwright/test";

test("file handoff preserves material and does not restart requirements discussion", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("导入需求规格", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存导入并查看" }),
  ).toBeDisabled();
  await page.screenshot({
    path: info.outputPath("projects.png"),
    fullPage: true,
  });
  const upload = page.locator('input[type="file"]');
  await upload.setInputFiles({
    name: "bad.json",
    mimeType: "application/json",
    buffer: Buffer.from("{invalid"),
  });
  await expect(page.getByText("校验未通过", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存导入并查看" }),
  ).toBeDisabled();
  await upload.setInputFiles({
    name: "requirements.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF"),
  });
  await expect(
    page.getByRole("alert").filter({ hasText: "当前支持 JSON" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存导入并查看" }),
  ).toBeDisabled();
  const text =
    "# 团队知识库需求\n支持文档检索与历史版本，不包含公开分享。\n<script>window.bad=true</script>";
  await upload.setInputFiles({
    name: "knowledge.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(text),
  });
  await page.getByLabel("项目名称").fill("知识库需求交接");
  await page.getByLabel("补充说明").fill("优先核实检索范围。");
  await expect(page.getByText("待整理材料", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "保存导入并查看" }).click();
  await expect(
    page.getByRole("heading", { name: "知识库需求交接" }),
  ).toBeVisible();
  await expect(page.locator(".material-preview pre")).toHaveText(text);
  await expect(
    page.getByText("优先核实检索范围。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "建立运行并核验需求" }),
  ).toBeDisabled();
  await expect(page.getByText("多体讨论", { exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await page.screenshot({
    path: info.outputPath("workstation.png"),
    fullPage: true,
  });
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出原文件" }).click();
  expect((await download).suggestedFilename()).toBe("knowledge.md");
  await page.getByRole("tab", { name: "任务分解", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "任务计划尚未生成" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("mobile import and navigation fit the viewport", async ({
  page,
}, info) => {
  await page.route("**/api/settings/agents", async route => route.fulfill({json:{agents:["Athena","Daedalus","Momus","Hephaestus","Argus","Hermes"].map(role_id => ({role_id,base_url:"",model:"",timeout_seconds:120,configured:false,has_api_key:false}))}}));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.screenshot({
    path: info.outputPath("mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .locator('input[type="file"]')
    .setInputFiles({
      name: "需求.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("任意业务需求材料，需回到上游确认。"),
    });
  await page.getByRole("button", { name: "保存导入并查看" }).click();
  await expect(
    page.getByRole("heading", { name: "需求交接内容" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("menuitem", { name: "协作角色" }).click();
  await expect(page.getByRole("heading", { name: "Athena" })).toBeVisible();
});
