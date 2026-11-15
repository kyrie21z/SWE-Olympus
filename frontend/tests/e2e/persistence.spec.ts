import { test, expect } from "@playwright/test";

test("a user prompt survives reload and never becomes an approved spec", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("#new").getByLabel("项目名称").fill("开放需求持久化");
  await page
    .locator("#new")
    .getByLabel("需求描述")
    .fill("为小团队构建一个排班工具，允许换班请求和人工审核。");
  await page.getByRole("button", { name: "创建需求草稿" }).click();
  await expect(page.locator(".prompt-content")).toContainText("排班工具");
  await page.reload();
  await expect(page.locator(".prompt-content")).toContainText("排班工具");
  await page.getByRole("button", { name: "编辑需求" }).click();
  await page
    .getByRole("dialog")
    .getByLabel("需求描述")
    .fill("排班工具：增加节假日偏好。");
  await page.getByRole("button", { name: "保存修改" }).click();
  await expect(page.locator(".prompt-content")).toContainText("节假日偏好");
  await page.reload();
  await expect(page.locator(".prompt-content")).toHaveText(
    "排班工具：增加节假日偏好。",
  );
  await expect(page.getByText("草稿 v2", { exact: false })).toBeVisible();
  await page.getByRole("tab", { name: "需求规格", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "需求规格尚未生成" }),
  ).toBeVisible();
});
