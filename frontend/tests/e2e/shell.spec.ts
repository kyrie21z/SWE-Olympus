import { test, expect } from "@playwright/test";
test("foundation shell opens the workstation and returns", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "每一个想法，都有清晰的下一步。" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(
    page.getByRole("heading", { name: "工作台基础已就绪" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "返回项目中心" }).click();
  await expect(
    page.getByRole("heading", { name: "SWE-Olympus 项目空间" }),
  ).toBeVisible();
});
test("foundation shell fits a mobile screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
