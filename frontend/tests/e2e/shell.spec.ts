import { test, expect } from "@playwright/test";

test("arbitrary prompts create separate editable drafts without invented output", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "欢迎来到 Olympus" }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("projects.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "创建需求草稿" }).click();
  await expect(page.getByText("请先描述你想做的软件")).toBeVisible();
  const prompt =
    "为自由职业者设计一个项目报价工具。\n支持报价版本比较、导出 PDF，不包含支付。";
  await page.getByLabel("项目名称").fill("自由职业者报价工具");
  await page.getByLabel("需求描述").fill(prompt);
  await page.getByLabel("约束与补充").fill("六人团队，八周，先完成 Web 版。");
  await page.getByRole("button", { name: "创建需求草稿" }).click();
  await expect(page.locator(".prompt-content")).toHaveText(prompt);
  await expect(page.getByText("尚未执行", { exact: true })).toBeVisible();
  await page.screenshot({
    path: info.outputPath("workstation.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "需求规格", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "需求规格尚未生成" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "原始需求" }).click();
  await page.getByRole("button", { name: "编辑需求" }).click();
  await page
    .getByRole("dialog")
    .getByLabel("需求描述")
    .fill(prompt + "\n增加多币种展示。");
  await page.getByRole("button", { name: "保存修改" }).click();
  await expect(page.locator(".prompt-content")).toContainText(
    "增加多币种展示。",
  );
  await expect(page.getByText("草稿 v2", { exact: false })).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出", exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(
    /^olympus-draft-.+\.json$/,
  );
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  await page.getByLabel("项目名称").fill("阅读笔记管理");
  await page
    .getByLabel("需求描述")
    .fill("管理阅读笔记，支持标签、搜索和 Markdown 导出。");
  await page.getByRole("button", { name: "创建需求草稿" }).click();
  await page.getByRole("menuitem", { name: "项目中心" }).click();
  await expect(page.locator(".ant-table-tbody .ant-table-row")).toHaveCount(2);
  await page.getByLabel("搜索项目").fill("报价");
  await expect(page.locator(".ant-table-tbody .ant-table-row")).toHaveCount(1);
  await page.getByRole("button", { name: "打开", exact: false }).click();
  await expect(page.locator(".prompt-content")).toContainText(
    "增加多币种展示。",
  );
  expect(errors).toEqual([]);
});

test("mobile navigation and workspace fit narrow screens", async ({
  page,
}, info) => {
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
    .getByLabel("需求描述")
    .fill("我要做一个团队知识库，支持检索与文档修订历史。");
  await page.getByRole("button", { name: "创建需求草稿" }).click();
  await expect(page.locator(".prompt-content")).toContainText("团队知识库");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("menuitem", { name: "协作角色" }).click();
  await expect(page.getByRole("heading", { name: "Athena" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
