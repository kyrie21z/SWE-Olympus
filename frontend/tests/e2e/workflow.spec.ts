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
  let planRequests = 0;
  let revisionComplete = false;
  const findings = ["检查重复预占", "检查库存守恒", "执行五轮并发验收", "检查库存快照隔离"];
  const advice = {finding_id:"ADVICE",message:"建议补充库存快照示例",severity:"important",status:"open"};
  const blockers = findings.map((message, index) => ({finding_id:`B-${index}`,message,severity:"blocker",status:"open"}));
  const planBlocker = {finding_id:"PLAN-B1",message:"任务缺少可验证产物",severity:"blocker",status:"open"};
  const planAdvice = {finding_id:"PLAN-A1",message:"建议统一任务命名",severity:"important",status:"open"};
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
      if (snapshot.stage === "plan_generating" && planRequests === 1) {
        snapshot = {...snapshot,stage:"plan_review_blocked",plan:bundle.plan,
          plan_review_record:{review:{verdict:"blocked"},submission:{findings:[planBlocker,planAdvice]}}};
      } else if (snapshot.stage === "plan_generating") {
        snapshot = {...snapshot,stage:"plan_pending_approval",plan:bundle.plan,plan_review:{verdict:"passed",findings:[]},
          plan_review_record:{review:{verdict:"passed"},submission:{findings:[{...planBlocker,status:"closed"},planAdvice]}},
          acceptance:{T01:{checks:{"check:T01":"assert 2 + 2 == 4"},fixtures:{},content_hash:"frozen-check-hash",author:"Momus"}}};
      }
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
    const next = {approve_requirement:"requirement_approved",generate_design:"design_generating",approve_design:"design_approved",generate_plan:"plan_generating",generate_plan_revision:"plan_generating",approve_plan:"plan_approved",execute:"awaiting_acceptance",accept_run:"completed"} as const satisfies Record<WorkflowAction, WorkflowSnapshot["stage"]>;
    snapshot = {...snapshot,stage:next[body.action as WorkflowAction]};
    if (body.action === "generate_design" && ++designRequests === 2) {
      snapshot.design_revision = {round:2,limit:3,started_at:new Date().toISOString(),finding_count:2};
    }
    if (body.action === "generate_plan" || body.action === "generate_plan_revision") planRequests++;
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
  await expect(page.getByText("还有 1 条计划阻断项待修复")).toBeVisible();
  await expect(page.getByRole("button", {name:"批准计划与验收检查",exact:true})).toHaveCount(0);
  await page.getByRole("button", {name:"修复计划阻断项"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准计划与验收检查");
  await expect(page.getByText("计划审查通过，保留 1 条建议")).toBeVisible();
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
  expect(actions).toEqual(["approve_requirement","generate_design","generate_design","approve_design","generate_plan","generate_plan","approve_plan","execute","accept_run"]);
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

test("planning reconnects after a lost response and shows Momus progress without resubmitting", async ({page}) => {
  test.setTimeout(30_000);
  let actions = 0, failedPoll = false, completed = false;
  let snapshot: WorkflowSnapshot = {
    workflow_id:"WF-reconnect",run_id:"RUN-reconnect",stage:"design_approved",error:null,
    requirement:example,design:bundle.design,design_review:null,plan:null,plan_review:null,
    executions:[],acceptance:{},allowed_actions:["generate_plan"],
  };
  await page.route("**/api/workflows**", async route => {
    if (route.request().url().endsWith("/actions")) {
      actions++;
      snapshot = {...snapshot,stage:"plan_generating",pending_action:"generate_plan",allowed_actions:[],progress:{role_id:"Momus",task_kind:"acceptance_definition",task_id:"T02",completed:1,total:8,message:"Momus 正在生成 T02 的验收检查"}};
      return route.abort("connectionreset");
    }
    if (route.request().method() === "GET" && snapshot.stage === "plan_generating") {
      if (!failedPoll) {failedPoll = true; return route.abort("connectionreset");}
      if (completed) snapshot = {...snapshot,stage:"plan_pending_approval",progress:null,pending_action:null,plan:bundle.plan,allowed_actions:["approve_plan"]};
    }
    return route.fulfill({json:snapshot});
  });
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles({name:"reconnect.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
  await page.getByRole("button", {name:"保存导入并查看"}).click();
  await page.getByLabel("在线 Agent", {exact:true}).check();
  await page.getByLabel("允许将这份构造样例用于测试运行").check();
  await page.getByRole("button", {name:"建立运行并核验需求"}).click();
  await page.getByRole("button", {name:"生成任务计划"}).click();
  await expect(page.getByText("后端连接暂时中断，正在自动重连", {exact:true})).toBeVisible();
  await expect(page.getByTestId("workflow-progress")).toContainText("Momus 正在生成 T02 的验收检查", {timeout:10_000});
  await expect(page.getByTestId("workflow-progress")).toContainText("已完成 1 / 8");
  await expect(page.getByText("后端连接暂时中断，正在自动重连", {exact:true})).toHaveCount(0);
  await expect(page.getByRole("button", {name:"刷新状态"})).toBeEnabled();
  expect(actions).toBe(1);
  completed = true;
  await page.getByRole("button", {name:"刷新状态"}).click();
  await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准计划与验收检查");
  await expect(page.getByTestId("workflow-progress")).toHaveCount(0);
  expect(actions).toBe(1);
});

for (const phase of ["design", "plan"] as const) {
  test(`${phase} review keeps repaired blockers in history and retains open advice`, async ({page}) => {
    const repairedMessage = "已完成的校验顺序修复";
    const openMessage = "建议补充终态幂等测试";
    const closure = "TASK-RESERVE.checks 已包含格式、冲突、存在性、库存的完整校验顺序";
    const canonical = {verdict:"passed",findings:[repairedMessage,openMessage]};
    const record = {review:canonical,submission:{findings:[
      {finding_id:"REPAIRED",message:repairedMessage,severity:"blocker",status:"closed",closure_reason:closure},
      {finding_id:"OPEN-ADVICE",message:openMessage,severity:"important",status:"open"},
    ]}};
    const snapshot: WorkflowSnapshot = {
      workflow_id:`WF-review-${phase}`,run_id:`RUN-review-${phase}`,stage:phase === "plan" ? "plan_pending_approval" : "design_pending_approval",
      error:null,requirement:example,design:bundle.design,design_review:phase === "design" ? record : null,
      plan:phase === "plan" ? bundle.plan : null,plan_review:canonical,plan_review_record:phase === "plan" ? record : undefined,
      executions:[],acceptance:{},allowed_actions:[],
    };
    await page.route("**/api/workflows**", route => route.fulfill({json:snapshot}));
    await page.goto("/");
    await page.locator('input[type="file"]').setInputFiles({name:`review-${phase}.json`,mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
    await page.getByRole("button", {name:"保存导入并查看"}).click();
    await page.getByRole("radio", {name:"离线样例"}).check();
    await page.getByLabel("允许将这份构造样例用于测试运行").check();
    await page.getByRole("button", {name:"建立运行并核验需求"}).click();
    await page.getByRole("tab", {name:phase === "plan" ? "任务分解" : "设计规格",exact:true}).click();
    const review = page.locator(".workflow-review:visible");
    const active = review.getByRole("list", {name:"未关闭的审查意见"});
    await expect(active).toContainText(openMessage);
    await expect(active).not.toContainText(repairedMessage);
    await expect(active.getByText("阻断", {exact:true})).toHaveCount(0);
    await expect(review.getByText("通过", {exact:true})).toBeVisible();
    await expect(review.getByText(repairedMessage, {exact:true})).not.toBeVisible();
    await review.getByText("已修复 1 项", {exact:true}).click();
    const resolved = review.getByRole("list", {name:"已修复的审查意见"});
    await expect(resolved).toContainText(repairedMessage);
    await expect(resolved).toContainText(closure);
    await expect(active).toContainText(openMessage);
    await review.getByText("查看完整独立审查报告", {exact:true}).click();
    await expect(review.locator("pre")).toContainText('"status": "closed"');
  });
}

test("legacy raw review findings remain visible when no structured record exists", async ({page}) => {
  const message = "旧版报告中的审查说明";
  await page.route("**/api/workflows**", route => route.fulfill({json:{
    workflow_id:"WF-legacy-review",run_id:"RUN-legacy-review",stage:"plan_pending_approval",error:null,
    requirement:example,design:bundle.design,design_review:null,plan:bundle.plan,
    plan_review:{verdict:"passed",findings:[message]},executions:[],acceptance:{},allowed_actions:[],
  }}));
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles({name:"legacy-review.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
  await page.getByRole("button", {name:"保存导入并查看"}).click();
  await page.getByRole("radio", {name:"离线样例"}).check();
  await page.getByLabel("允许将这份构造样例用于测试运行").check();
  await page.getByRole("button", {name:"建立运行并核验需求"}).click();
  await page.getByRole("tab", {name:"任务分解",exact:true}).click();
  await expect(page.getByRole("list", {name:"未关闭的审查意见"})).toContainText(message);
  await expect(page.getByText("已修复 1 项", {exact:true})).toHaveCount(0);
});

for (const stage of ["execution_blocked", "error"] as const) {
  test(`${stage} source opens its existing revision and never resubmits old execution`, async ({page}) => {
    let creates = 0, actions = 0;
    const source: WorkflowSnapshot = {
      workflow_id:"WF-source",run_id:"RUN-source",stage,error:{code:"TASKS_BLOCKED",message:"旧运行保留失败证据"},
      requirement:example,design:bundle.design,design_review:null,plan:bundle.plan,plan_review:null,
      executions:[{task_id:"TASK-IFACE-SERVICE",runtime:{state:"failed"},execution:{state:"failed",check_outputs:[
        {check_id:"Passed signatures",exit_code:0},{check_id:"Source metadata import_roots is empty",exit_code:1},
      ]}}],acceptance:{},plan_bound:true,successor_workflow_id:"WF-revision",successor_run_id:"RUN-revision",
      retry_safe:stage === "error",retry_action:stage === "error" ? "execute" : null,allowed_actions:["execute"],
    };
    const child: WorkflowSnapshot = {
      workflow_id:"WF-revision",run_id:"RUN-revision",stage:"plan_pending_approval",error:null,
      requirement:example,design:bundle.design,design_review:null,plan:bundle.plan,plan_review:null,
      executions:[],acceptance:{},plan_bound:false,source_workflow_id:"WF-source",source_run_id:"RUN-source",allowed_actions:["approve_plan"],
    };
    await page.route("**/api/workflows**", route => {
      const request = route.request();
      if (request.method() === "POST") {
        if (new URL(request.url()).pathname.endsWith("/actions")) actions++;
        else creates++;
        return route.fulfill({json:source});
      }
      return route.fulfill({json:new URL(request.url()).pathname.endsWith("/WF-revision") ? child : source});
    });
    await page.goto("/");
    await page.locator('input[type="file"]').setInputFiles({name:`linked-${stage}.json`,mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
    await page.getByRole("button", {name:"保存导入并查看"}).click();
    await page.getByRole("radio", {name:"离线样例"}).check();
    await page.getByLabel("允许将这份构造样例用于测试运行").check();
    await page.getByRole("button", {name:"建立运行并核验需求"}).click();
    await expect(page.getByRole("button", {name:"继续执行",exact:true})).toHaveCount(0);
    await expect(page.getByRole("button", {name:"重试当前阶段",exact:true})).toHaveCount(0);
    await expect(page.getByTestId("workflow-failure")).toContainText("TASK-IFACE-SERVICE");
    await expect(page.getByTestId("workflow-failure")).toContainText("Source metadata import_roots is empty");
    await expect(page.getByTestId("workflow-failure")).not.toContainText("Passed signatures");
    await page.getByRole("button", {name:"打开修订后的运行",exact:true}).click();
    await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准计划与验收检查");
    await expect(page.getByText("WF-revision", {exact:true})).toBeVisible();
    await expect(page.getByRole("button", {name:"批准计划与验收检查",exact:true})).toBeDisabled();
    await expect(page.getByTestId("workflow-failure")).toHaveCount(0);
    await expect(page.getByRole("button", {name:"查看来源运行",exact:true})).toBeVisible();
    await page.getByRole("button", {name:"查看来源运行",exact:true}).click();
    await expect(page.getByRole("button", {name:"打开修订后的运行",exact:true})).toBeVisible();
    await expect(page.getByRole("button", {name:"继续执行",exact:true})).toHaveCount(0);
    await page.getByRole("button", {name:"打开修订后的运行",exact:true}).click();
    await page.reload();
    await expect(page.getByTestId("workflow-stage")).toHaveText("等待批准计划与验收检查");
    await expect(page.getByText("WF-revision", {exact:true})).toBeVisible();
    expect(creates).toBe(1);
    expect(actions).toBe(0);
  });
}

for (const stage of ["plan_review_blocked", "error"] as const) {
  test(`${stage} polling never repairs a plan without an explicit button click`, async ({page}) => {
    let gets = 0, actions = 0;
    let snapshot: WorkflowSnapshot = {
      workflow_id:"WF-no-auto-repair",run_id:"RUN-no-auto-repair",stage,
      error:stage === "error" ? {code:"OUTPUT_SCHEMA_ERROR",message:"等待显式重试",action:"generate_plan"} : null,
      requirement:example,design:bundle.design,design_review:null,plan:bundle.plan,
      plan_review_record:{review:{verdict:"blocked"},submission:{findings:[{finding_id:"OPEN",message:"等待明确修复",severity:"blocker",status:"open"}]}},
      plan_review:null,executions:[],acceptance:{},allowed_actions:["generate_plan"],
      retry_safe:stage === "error",retry_action:stage === "error" ? "generate_plan" : null,
    };
    await page.route("**/api/workflows**", route => {
      const request = route.request();
      if (request.method() === "GET") gets++;
      if (new URL(request.url()).pathname.endsWith("/actions")) {
        actions++;
        expect(request.postDataJSON().action).toBe("generate_plan");
        snapshot = {...snapshot,stage:"plan_generating",error:null,allowed_actions:[],retry_action:null,retry_safe:false};
      }
      return route.fulfill({json:snapshot});
    });
    await page.goto("/");
    await page.locator('input[type="file"]').setInputFiles({name:`no-auto-${stage}.json`,mimeType:"application/json",buffer:Buffer.from(JSON.stringify(example))});
    await page.getByRole("button", {name:"保存导入并查看"}).click();
    await page.getByRole("radio", {name:"离线样例"}).check();
    await page.getByLabel("允许将这份构造样例用于测试运行").check();
    await page.getByRole("button", {name:"建立运行并核验需求"}).click();
    await expect.poll(() => gets, {timeout:8000}).toBeGreaterThanOrEqual(3);
    expect(actions).toBe(0);
    await expect(page.getByTestId("workflow-stage")).toHaveText(stage === "error" ? "运行出错" : "计划审查阻断");
    await page.getByRole("button", {name:stage === "error" ? "重试当前阶段" : "修复计划阻断项",exact:true}).click();
    await expect(page.getByTestId("workflow-stage")).toHaveText("Daedalus 正在规划并冻结验收检查");
    expect(actions).toBe(1);
  });
}
