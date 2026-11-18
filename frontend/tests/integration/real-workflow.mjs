// This script intentionally uses the real application HTTP API, without page.route.
import { chromium, expect as baseExpect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const [baseUrl, modelUrl, requirementPath, outputDirectory] = process.argv.slice(2);
if (!baseUrl || !modelUrl || !requirementPath || !outputDirectory) {
  throw new Error("Expected frontend URL, local model URL, REQ path and evidence directory");
}
await mkdir(outputDirectory, { recursive: true });
const expect = baseExpect.configure({ timeout: 30_000 });
const browser = await chromium.launch({
  channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const pageErrors = [];
const apiResponses = [];
const actions = [];
let workflow;
page.on("pageerror", error => pageErrors.push(error.message));
page.on("response", response => {
  const url = new URL(response.url());
  if (url.pathname.startsWith("/api/")) apiResponses.push({ path: url.pathname, status: response.status() });
});
page.on("request", request => {
  if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/actions")) {
    actions.push(request.postDataJSON().action);
  }
});
const screenshots = {};
async function screenshot(name) {
  const output = path.join(outputDirectory, `${name}.png`);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: output, fullPage: true });
  screenshots[name] = output;
}
async function clickStage(button, expected, timeout = 45_000) {
  const target = page.getByRole("button", { name: button, exact: true });
  await expect(target).toBeEnabled();
  await target.click();
  await expect(page.getByTestId("workflow-stage")).toHaveText(expected, { timeout });
}
try {
  // Five other roles were seeded via the real API into a temporary test database.
  // Athena is configured and probed using the browser form itself.
  await page.goto(`${baseUrl}/#agents`);
  const athena = page.getByTestId("agent-settings-Athena");
  await athena.getByLabel("API 根地址").fill(modelUrl);
  await athena.getByLabel("模型名称").fill("local-Athena");
  await athena.getByLabel("API Key", { exact: true }).fill("browser-test-key-Athena");
  await athena.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(athena.getByText("已配置", { exact: true })).toBeVisible();
  await expect(athena.getByLabel("API Key", { exact: true })).toHaveValue("");
  await athena.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(athena.getByText("连接成功，模型已返回有效 JSON。", { exact: true })).toBeVisible();
  await screenshot("agent-settings");
  const browserStorage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(browserStorage).not.toContain("browser-test-key");

  await page.goto(`${baseUrl}/`);
  await page.locator('input[type="file"]').setInputFiles(requirementPath);
  await expect(page.getByText("构造样例", { exact: true })).toBeVisible();
  await page.getByLabel("项目名称").fill("真实后端八任务联调");
  await page.getByRole("button", { name: "保存导入并查看", exact: true }).click();
  await page.getByLabel("允许将这份构造样例用于测试运行").check();
  const createdResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/workflows" && response.request().method() === "POST");
  await page.getByRole("button", { name: "建立运行并核验需求", exact: true }).click();
  const created = await createdResponse;
  expect(created.status()).toBe(200);
  workflow = await created.json();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待需求接纳");
  await clickStage("接纳此需求用于当前运行", "需求已接纳");
  await clickStage("生成设计规格", "等待批准设计");
  // A refresh must restore the same workflow rather than creating a second run.
  await page.reload();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准设计");
  await clickStage("批准当前设计", "设计已批准");
  await clickStage("生成任务计划", "等待批准计划与验收检查");
  await expect(page.getByRole("button", { name: "批准计划与验收检查", exact: true })).toBeDisabled();
  await page.getByText("T01 · 1 项检查", { exact: true }).click();
  await expect(page.getByText('assert set(states()) == {"CONFIRMED", "WAITLISTED", "CANCELLED"}', { exact: false })).toBeVisible();
  await screenshot("frozen-acceptance");
  await page.getByLabel("我已审阅当前计划及冻结的验收检查").check();
  await clickStage("批准计划与验收检查", "计划已批准");
  await clickStage("执行全部任务", "等待最终验收", 90_000);
  await clickStage("确认最终验收", "已完成");
  await expect(page.getByText("当前运行已完成人工最终验收", { exact: true })).toBeVisible();
  await screenshot("workflow-completed");
  const finalResponse = await page.request.get(`${baseUrl}/api/workflows/${workflow.workflow_id}`);
  expect(finalResponse.status()).toBe(200);
  const completed = await finalResponse.json();
  expect(completed.stage).toBe("completed");
  expect(completed.snapshot.tasks).toHaveLength(8);
  expect(completed.snapshot.tasks.every(task => task.state === "accepted" && task.verification_status === "passed")).toBe(true);
  expect(completed.executions).toHaveLength(8);
  expect(completed.executions.every(item => item.execution.tool_invocations > 0)).toBe(true);

  await page.getByRole("link", { name: "查看此运行控制台", exact: true }).click();
  await expect(page.getByTestId("p6-console")).toBeVisible();
  await expect(page.locator(".p6-stat-grid .ant-statistic-content").first()).toHaveText(/8\s*\/\s*8/);
  await expect(page.locator("[data-p6-task]")).toHaveCount(8);
  await expect(page.getByText("5 / 5 已验证", { exact: true })).toBeVisible();
  const evidenceResponse = await page.request.get(`${baseUrl}/api/runs/${workflow.run_id}/executions/T03`);
  expect(evidenceResponse.status()).toBe(200);
  const execution = await evidenceResponse.json();
  expect(execution.execution.model_mode).toBe("real_model");
  expect(execution.execution.check_outputs.every(check => check.exit_code === 0)).toBe(true);
  expect(pageErrors).toEqual([]);
  expect(apiResponses.filter(response => response.status >= 400)).toEqual([]);
  expect(actions).toEqual(["approve_requirement", "generate_design", "approve_design", "generate_plan", "approve_plan", "execute", "accept_run"]);
  await screenshot("run-console");
  await writeFile(path.join(outputDirectory, "executions.json"), JSON.stringify(completed.executions, null, 2));
  const report = {
    passed: true, workflow_id: workflow.workflow_id, run_id: workflow.run_id,
    actions, tasks: { accepted: 8, required: 8 }, api_replay: false,
    provider: "real-http-to-local-scripted-model", online_model_evaluated: false,
    tools: "real-python-subprocess", browser: process.env.PLAYWRIGHT_CHANNEL || "chromium",
    page_errors: pageErrors, api_responses: apiResponses, screenshots,
  };
  await writeFile(path.join(outputDirectory, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: true, ...report.tasks, report: path.join(outputDirectory, "report.json") }));
} catch (error) {
  await screenshot("failure").catch(() => {});
  let state;
  if (workflow) {
    state = await page.request.get(`${baseUrl}/api/workflows/${workflow.workflow_id}`)
      .then(response => response.json()).catch(() => null);
  }
  await writeFile(path.join(outputDirectory, "report.json"), JSON.stringify({
    passed: false, error: String(error), workflow: state,
    page_errors: pageErrors, api_responses: apiResponses, actions, screenshots,
  }, null, 2));
  throw error;
} finally {
  await browser.close();
}
