import { expect, test } from "@playwright/test";
import commandFixture from "../../../fixtures/http/command_start.json" with {
  type: "json",
};
import viewFixture from "../../../fixtures/http/view.json" with {
  type: "json",
};

const runId = "RUN-campus";

async function installApiReplay(page: import("@playwright/test").Page) {
  const commands: Array<Record<string, unknown>> = [];
  const state = { eventFailure: false, rejectNextCommand: false };
  const view = viewFixture.body;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === `/api/runs/${runId}/view`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(view),
      });
      return;
    }
    if (url.pathname === `/api/runs/${runId}/events`) {
      if (state.eventFailure) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            error: { code: "RECOVERY_GAP", message: "event replay unavailable" },
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
      return;
    }
    if (url.pathname === `/api/runs/${runId}/trail`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: runId,
          assignments: {},
          entries: [],
          repairs: [],
        }),
      });
      return;
    }
    if (url.pathname === `/api/runs/${runId}/executions/T01`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: runId,
          task_id: "T01",
          attempt_id: "ATT-1",
          is_current: true,
          runtime: view.snapshot.tasks[0],
          execution: {
            schema_version: "1.0",
            record_id: "ER-1",
            attempt_id: "ATT-1",
            task_id: "T01",
            inputs: [],
            tools: [{ name: "registered_local_python" }],
            artifacts: [{ artifact_id: "artifact:T01" }],
            diff: "+ lock.acquire()",
            check_outputs: [
              {
                check_id: "check:T01",
                command: "python frozen_check.py",
                exit_code: 0,
                output: "ok",
              },
            ],
            elapsed_ms: 12,
            call_count: 2,
            retry_history: [],
            mode: "real_tool",
            state: "succeeded",
            tool_invocations: 2,
            tool_mode: "real_tool",
            model_mode: "mock",
            checks_passed: ["check:T01"],
            review: null,
          },
          review_requests: [],
        }),
      });
      return;
    }
    if (url.pathname === "/api/commands" && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      commands.push(body);
      if (state.rejectNextCommand) {
        state.rejectNextCommand = false;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: {
            "X-Olympus-Durability": "memory",
            "X-Olympus-Recorded-Events": "0",
          },
          body: JSON.stringify({
            ...commandFixture.body,
            operation_id: body.operation_id,
            run_id: runId,
            status: "rejected",
            reason_code: "STALE_REVISION",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: {
          "X-Olympus-Durability": "memory",
          "X-Olympus-Recorded-Events": "1",
        },
        body: JSON.stringify({
          ...commandFixture.body,
          operation_id: body.operation_id,
          run_id: runId,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "RUN_NOT_FOUND", message: "unexpected route" },
      }),
    });
  });
  return { commands, state };
}

test("P6 console reads P5 views, task evidence, and command receipts", async ({
  page,
}, info) => {
  test.setTimeout(45_000);
  const { commands, state } = await installApiReplay(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto("/#console");
  await expect(page.getByTestId("p6-console")).toBeVisible();
  await expect(page.getByText("只读业务 DAG", { exact: true })).toBeVisible();
  await expect(page.locator('[data-p6-task="T01"]')).toHaveCount(1);
  await expect(
    page.locator(".p6-stat-grid .ant-statistic-content").first(),
  ).toHaveText(/0\s*\/\s*8/);
  await expect(page.getByText("+ lock.acquire()", { exact: true })).toBeVisible();
  await page.screenshot({
    path: info.outputPath("p6-console.png"),
    fullPage: true,
  });

  await page.getByRole("button", { name: "人工命令" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByLabel("命令类型").click();
  await page.getByText("恢复运行", { exact: true }).click();
  await page.getByRole("button", { name: "提交命令" }).click();
  await expect(page.getByTestId("p6-command-receipt")).toContainText(
    "accepted",
  );
  await expect(page.getByTestId("p6-command-receipt")).toContainText("memory");
  await page.getByRole("button", { name: "再次提交同一 operation" }).click();
  await expect.poll(() => commands.length).toBe(2);
  expect(commands[0].operation_id).toBe(commands[1].operation_id);

  await page.locator(".ant-modal-close").click();
  state.rejectNextCommand = true;
  await page.getByRole("button", { name: "人工命令" }).click();
  await page.getByLabel("命令类型").click();
  await page.getByText("恢复运行", { exact: true }).click();
  await page.getByRole("button", { name: "提交命令" }).click();
  await expect(page.getByTestId("p6-command-receipt")).toContainText(
    "STALE_REVISION",
  );
  await page.screenshot({
    path: info.outputPath("p6-command.png"),
    fullPage: true,
  });
  await page.locator(".ant-modal-close").click();

  state.eventFailure = true;
  await expect(page.getByText("数据可能过期", { exact: true })).toBeVisible({
    timeout: 8_000,
  });
  state.eventFailure = false;
  await page.getByRole("button", { name: "刷新运行视图" }).click();
  await expect(page.getByText("事件已同步", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  expect(errors).toEqual([]);
});

test("P6 console fits a mobile viewport with internal scrolling", async ({
  page,
}, info) => {
  await installApiReplay(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#console");
  await expect(page.getByTestId("p6-console")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("p6-mobile.png"),
    fullPage: true,
  });
});
