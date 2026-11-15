import { test, expect } from "@playwright/test";
test("project shell and fixture switching remain honest about verification", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "每一个想法，都有清晰的下一步。" }),
  ).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('projects.png'), fullPage: true});
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByText("1 / 8", { exact: true })).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('workstation.png'), fullPage: true});
  await page.getByLabel("样例场景").selectOption("risk_accepted");
  await expect(page.getByText("0 / 8", { exact: true })).toBeVisible();
  await expect(
    page.getByText("未验证 · 人工接受风险", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "依赖清单", exact: true }).click();
  await expect(page.locator("tbody tr")).toHaveCount(8);
  await page.getByRole("button", { name: "事件记录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "等待接入真实运行事件" }),
  ).toBeVisible();
});
test("mobile layout fits viewport", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "进入工作台" }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({path: testInfo.outputPath('mobile.png'), fullPage: true});
});
