import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { AGENT_ROLES, type AgentSetting, type WorkflowAction, type WorkflowSnapshot } from "../../src/workflow/client";
import { NEXT_ACTION } from "../../src/workflow/model";

const example = JSON.parse(readFileSync(new URL("../../public/examples/requirement.json", import.meta.url), "utf8"));
const bundle = JSON.parse(readFileSync(new URL("../../../fixtures/contracts/valid.json", import.meta.url), "utf8"));

test("agent API settings keep role configurations independent and keys out of browser storage", async ({page}) => {
  const settings: Record<string, AgentSetting> = Object.fromEntries(AGENT_ROLES.map(role => [role, {role_id:role,base_url:"",model:"",timeout_seconds:120,configured:false,has_api_key:false}]));
  const saved: string[] = [], tested: string[] = [];
  await page.route("**/api/settings/agents**", async route => {
    const path = new URL(route.request().url()).pathname;
    const role = path.split("/")[4];
    if (route.request().method() === "GET") return route.fulfill({json:{agents:Object.values(settings)}});
    if (path.endsWith("/test")) {tested.push(role); return route.fulfill({json:{ok:true,message:"连接成功"}});}
    const body = route.request().postDataJSON();
    if (path.endsWith("/models")) {
      expect(body).toEqual({base_url:"https://provider.example/v1",api_key:"test-secret-never-store"});
      return route.fulfill({json:{models:["design-model"]}});
    }
    saved.push(role);
    settings[role] = {...settings[role],base_url:body.base_url,model:body.model,timeout_seconds:body.timeout_seconds,has_api_key:Boolean(body.api_key) || settings[role].has_api_key,configured:true};
    return route.fulfill({json:settings[role]});
  });
  await page.goto("/#agents");
  const athena = page.getByTestId("agent-settings-Athena");
  await athena.getByLabel("API 根地址").fill("https://provider.example/v1");
  await athena.getByLabel("API Key", {exact:true}).fill("test-secret-never-store");
  await athena.getByRole("button", {name:"获取模型列表"}).click();
  const modelPicker = athena.getByRole("combobox", {name:"从模型列表选择"});
  await modelPicker.click();
  await modelPicker.press("ArrowDown");
  await modelPicker.press("Enter");
  await expect(athena.getByLabel("模型名称")).toHaveValue("design-model");
  await athena.getByRole("button", {name:"保存配置"}).click();
  await expect(athena.getByText("已配置", {exact:true})).toBeVisible();
  await expect(athena.getByLabel("API Key", {exact:true})).toHaveValue("");
  await athena.getByRole("button", {name:"测试连接"}).click();
  await expect(athena.getByText("连接成功", {exact:true})).toBeVisible();
  await expect(athena.getByText("使用已保存密钥。这是短请求测试。", {exact:true})).toBeVisible();
  await athena.getByLabel("模型名称").fill("modified-model");
  await expect(athena.getByText("连接成功", {exact:true})).toHaveCount(0);
  await expect(athena.getByRole("button", {name:"测试连接"})).toBeDisabled();
  expect(saved).toEqual(["Athena"]);
  expect(tested).toEqual(["Athena"]);
  await expect(page.getByTestId("agent-settings-Argus").getByText("未配置", {exact:true})).toBeVisible();
  await expect(page.getByTestId("agent-settings-Argus").getByRole("button", {name:"测试连接"})).toBeDisabled();
  expect(await page.evaluate(() => JSON.stringify({local:{...localStorage},session:{...sessionStorage}}))).not.toContain("test-secret-never-store");
  await page.reload();
  await expect(athena.getByLabel("模型名称")).toHaveValue("design-model");
  await expect(athena.getByLabel("API Key", {exact:true})).toHaveValue("");
});

test("workflow requires explicit human gates, shows frozen checks, and resumes after refresh", async ({page}) => {
  test.setTimeout(60_000);
  const actions: WorkflowAction[] = [];
  let created = false;
  let designRequests = 0;
  let revisionComplete = false;
  const findings = ["检查重复预占", "检查库存守恒", "执行五轮并发验收", "检查库存快照隔离"];
  const advice = {finding_id:"ADVICE",message:"建议补充库存快照示例",severity:"important",status:"open"};
  const blockers = findings.map((message, index) => ({finding_id:`B-${index}`,message,severity:"blocker",status:"open"}));
  let snapshot: WorkflowSnapshot = {
    workflow_id:"WF-browser-contract",run_id:"RUN-browser-contract",stage:"requirement_pending",error:null,
    requirement:example,design:null,design_review:null,plan:null,plan_review:null,acceptance:{},executions:[],
  };
  const view = () => ({...snapshot,allowed_actions:NEXT_ACTION[snapshot.stage] ? [NEXT_ACTION[snapshot.stage]!.action] : []});
  await page.route("**/api/workflows**", async route => {
    const request = route.request();
    if (request.method() === "GET") {
      if (snapshot.stage === "design_generating" && designRequests === 1) {
        snapshot = {...snapshot,stage:"design_review_blocked",design:bundle.design,
          design_review:{review:{verdict:"blocked"},submission:{findings:[...blockers,advice]}}};
      } else if (snapshot.stage === "design_generating" && revisionComplete) {
        snapshot = {...snapshot,stage:"design_pending_approval",design:{...bundle.design,version:3},
          design_review:{review:{verdict:"passed"},submission:{findings:[advice]}}};
      }
      if (snapshot.stage === "plan_generating") snapshot = {...snapshot,stage:"plan_pending_approval",plan:bundle.plan,plan_review:{verdict:"passed",findings:[]},acceptance:{T01:{checks:{"check:T01":"assert 2 + 2 == 4"},fixtures:{},content_hash:"frozen-check-hash",author:"Momus"}}};
      return route.fulfill({json:view()});
    }
    const body = request.postDataJSON();
    if (!request.url().endsWith("/actions")) {
      expect(body.allow_synthetic).toBe(true);
      expect(body.model_mode).toBe("offline");
      expect(body.requirement.object_id).toBe(example.object_id);
      created = true;
      return route.fulfill({json:view()});
    }
    actions.push(body.action);
    const next = {approve_requirement:"requirement_approved",generate_design:"design_generating",approve_design:"design_approved",generate_plan:"plan_generating",approve_plan:"plan_approved",execute:"awaiting_acceptance",accept_run:"completed"} as const;
    snapshot = {...snapshot,stage:next[body.action as WorkflowAction]};
    if (body.action === "generate_design" && ++designRequests === 2) {
      snapshot.design_revision = {round:2,limit:3,started_at:new Date().toISOString(),finding_count:2};
    }
    if (body.action === "execute") snapshot.executions = [{task_id:"T01",runtime:{state:"accepted"},execution:{checks_passed:["check:T01"],tool_mode:"real_tool",review:{verdict:"passed"}}}];
    return route.fulfill({json:view()});
  });
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles({name:"test-requirement.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
  await page.getByRole("button", {name:"保存导入并查看"}).click();
  await expect(page.getByRole("button", {name:"建立运行并核验需求"})).toBeDisabled();
  await page.getByLabel("允许将这份构造样例用于测试运行").check();
  await expect(page.getByRole("button", {name:"建立运行并核验需求"})).toBeDisabled();
  await page.getByRole("radio", {name:"离线样例"}).check();
  await page.getByRole("button", {name:"建立运行并核验需求"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待需求接纳");
  await expect(page.getByRole("link", {name:"查看此运行控制台"})).toHaveCount(0);
  expect(created).toBe(true); expect(actions).toEqual([]);
  await page.getByRole("button", {name:"接纳此需求用于当前运行"}).click();
  await page.getByRole("button", {name:"生成设计规格",exact:true}).click();
  await expect(page.getByText("还有 4 条设计阻断项待修复")).toBeVisible();
  await expect(page.getByText(findings[3], {exact:true}).first()).toBeVisible();
  await expect(page.getByRole("button", {name:"批准当前设计"})).toHaveCount(0);
  await page.getByRole("button", {name:"修复阻断项并复审"}).click();
  await expect(page.getByRole("status")).toContainText("自动修订第 2 轮（最多 3 轮），本轮处理 2 条阻断项。");
  revisionComplete = true;
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准设计");
  await expect(page.getByText("设计审查通过，保留 1 条建议")).toBeVisible();
  expect(actions).toEqual(["approve_requirement","generate_design","generate_design"]);
  await expect(page.getByRole("button", {name:"批准当前设计"})).toBeEnabled();
  await page.reload();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准设计");
  await page.getByRole("button", {name:"批准当前设计"}).click();
  await page.getByRole("button", {name:"生成任务计划"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准计划与验收检查");
  await expect(page.getByRole("button", {name:"批准计划与验收检查",exact:true})).toBeDisabled();
  await page.getByText("T01 · 1 项检查", {exact:true}).click();
  await expect(page.getByText("assert 2 + 2 == 4", {exact:true})).toBeVisible();
  await page.getByLabel("我已审阅当前计划及冻结的验收检查").check();
  await page.getByRole("button", {name:"批准计划与验收检查",exact:true}).click();
  await page.getByRole("button", {name:"执行全部任务"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待最终验收");
  await expect(page.getByRole("link", {name:"打开运行控制台",exact:true})).toHaveAttribute("href","?run=RUN-browser-contract#console");
  expect(actions).not.toContain("accept_run");
  await page.getByRole("button", {name:"确认最终验收"}).click();
  await expect(page.getByText("当前运行已完成人工最终验收", {exact:true})).toBeVisible();
  expect(actions).toEqual(["approve_requirement","generate_design","generate_design","approve_design","generate_plan","approve_plan","execute","accept_run"]);
});

test("the console handles missing and unapproved runs as empty states", async ({page}) => {
  let calls = 0;
  await page.route("**/api/runs/*/view", async route => {
    calls++;
    const waiting = new URL(route.request().url()).pathname.includes("RUN-waiting");
    return route.fulfill({status: waiting ? 409 : 404, json:{error:{code: waiting ? "PLAN_NOT_BOUND" : "RUN_NOT_FOUND", message:"unavailable"}}});
  });
  await page.goto("/#console");
  await expect(page.getByText("尚未选择运行")).toBeVisible();
  expect(calls).toBe(0);
  await page.goto("/?run=RUN-waiting#console");
  await expect(page.getByText("任务计划尚未批准")).toBeVisible();
  await expect(page.getByRole("link", {name:"返回工作台"})).toBeVisible();
  await page.goto("/?run=RUN-missing#console");
  await expect(page.getByText("找不到这次运行")).toBeVisible();
});

test("an unknown model outcome remains blocked without a blind retry action", async ({page}) => {
  let actions = 0;
  await page.route("**/api/workflows**", async route => {
    if (route.request().url().endsWith("/actions")) actions++;
    return route.fulfill({json:{workflow_id:"WF-unknown",run_id:"RUN-unknown",stage:"error",error:{code:"OUTCOME_UNKNOWN",message:"提供方结果未知，请先核对"},requirement:example,design:null,design_review:null,plan:null,plan_review:null,executions:[],acceptance:{},retry_safe:false,retry_action:null,allowed_actions:[]}});
  });
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles({name:"unknown.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
  await page.getByRole("button", {name:"保存导入并查看"}).click();
  await page.getByLabel("允许将这份构造样例用于测试运行").check();
  await page.getByRole("radio", {name:"在线 Agent"}).check();
  await page.getByRole("button", {name:"建立运行并核验需求"}).click();
  await expect(page.getByText("OUTCOME_UNKNOWN：提供方结果未知，请先核对", {exact:true})).toBeVisible();
  await expect(page.getByRole("button", {name:"重试当前阶段"})).toHaveCount(0);
  await expect(page.getByRole("button", {name:"生成设计规格",exact:true})).toHaveCount(0);
  await page.getByRole("button", {name:"刷新状态"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("运行出错");
  expect(actions).toBe(0);
});
