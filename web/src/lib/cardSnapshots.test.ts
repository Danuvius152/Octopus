import { beforeEach, expect, it } from "vitest";
import { useSessionStore, type BgTask, type Delegation, type SubagentRun, type ResearchJob } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { applySessionSnapshot } from "./hydrateSession";

beforeEach(() => useSessionStore.setState({ subagents: {}, bgTasks: {}, research: {}, delegations: {} }));

it("retains committed return diagnostics when an older delegation list arrives", () => {
  const store = useSessionStore.getState();
  const running: Delegation = { delegation_id: "child", work_id: "work", parent_session_id: "s", state: "running",
    sub_session_id: "child", target_agent_id: "agent", target_agent_name: "Helper", request: "Original work",
    created_at: "now", finished_at: null, error: null };
  store.setDelegations("s", [running]);
  const before = useSessionStore.getState().delegations.s;
  handleWsMessage({ type: "delegation_delivery", session_id: "s", delegation: {
    ...running, state: "completed", terminal_disposition: "unknown", terminal_delivery_error: "Original ACK lost",
  } });
  store.setDelegations("s", [running], before);
  expect(useSessionStore.getState().delegations.s[0]).toMatchObject({
    state: "completed", terminal_disposition: "unknown", terminal_delivery_error: "Original ACK lost",
  });
});

it("retains a newer subagent result across an older detail response", () => {
  const store = useSessionStore.getState();
  const running = { tool_call_id: "call", task_id: "child", status: "running", name: "Worker" } as SubagentRun;
  store.upsertSubagent("s", running);
  const before = useSessionStore.getState();
  store.upsertSubagent("s", { ...running, status: "completed", summary: "new answer" });
  const dispatch = { generation: "cards", revision: 0, paused: false, active_request_id: null, items: [] };
  applySessionSnapshot("s", { messages: [], subagents: [running], dispatch_state: dispatch, active_attempt: null }, before);
  expect(useSessionStore.getState().subagents.s.call.summary).toBe("new answer");
  const nextStart = useSessionStore.getState();
  applySessionSnapshot("s", { messages: [], subagents: [{ ...running, summary: "new round" }], dispatch_state: dispatch, active_attempt: null }, nextStart);
  expect(useSessionStore.getState().subagents.s.call.status).toBe("completed");
});

it("retains a delivery observation through list and completion updates while a detail request is pending", () => {
  const store = useSessionStore.getState();
  const original = { id: "bg", status: "completed", delivery_disposition: null, delivery_error: null } as BgTask;
  store.setBgTasks("s", [original]);
  handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "bg",
    delivery_disposition: "unknown", delivery_error: "ACK lost" });
  const listStart = useSessionStore.getState().bgTasks.s;
  store.setBgTasks("s", [{ ...original, delivery_disposition: "unknown", delivery_error: "ACK lost" }], listStart);
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "bg", status: "completed", exit_code: 0, completed_at: "now" });
  store.upsertBgTask("s", { ...original, stdout: "Full fetched output" }, original);
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({
    delivery_disposition: "unknown", delivery_error: "ACK lost", stdout: "Full fetched output",
  });
  // A detail request begun after that observation may report its later
  // reconciliation. The old event alone must not freeze unknown forever.
  const detailStart = useSessionStore.getState().bgTasks.s[0];
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "bg", status: "completed", exit_code: 0, completed_at: "now" });
  store.upsertBgTask("s", { ...original, stdout: "Later fetched output" }, detailStart);
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({
    delivery_disposition: null, delivery_error: null, stdout: "Later fetched output",
  });
});

it("merges background and research snapshots using request-start entities", () => {
  const store = useSessionStore.getState();
  const bg = { id: "bg", status: "running" } as BgTask;
  const research = { id: "research", question: "q", status: "running" } as ResearchJob;
  store.setBgTasks("s", [bg]);
  store.setResearch("s", [research]);
  const before = useSessionStore.getState();
  store.upsertBgTask("s", { ...bg, status: "completed", stdout: "new result" });
  store.upsertResearch("s", { id: "research", status: "completed", verified: 2 });
  store.setBgTasks("s", [bg], before.bgTasks.s);
  store.setResearch("s", [research], before.research.s);
  expect(useSessionStore.getState().bgTasks.s[0].stdout).toBe("new result");
  expect(useSessionStore.getState().research.s[0].verified).toBe(2);
});
