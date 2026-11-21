import { beforeEach, expect, it } from "vitest";
import { useSessionStore, type BgTask, type Delegation, type SubagentRun, type ResearchJob } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { applySessionSnapshot } from "./hydrateSession";

beforeEach(() => useSessionStore.setState({ subagents: {}, bgTasks: {}, research: {}, delegations: {} }));

const subagentDispatch = { generation: "cards", revision: 0, paused: false, active_request_id: null,
  active_operation: null, items: [] };

it("reuses the subagent collection across five equivalent complete snapshots", () => {
  const run: SubagentRun = { task_id: "child", card_id: "card", status: "running", name: "Worker",
    description: "Read history", prompt: "Check the history", summary: "", steps: ["Read history"],
    runtime_metadata: { request_id: "request", attempt_id: "attempt" } };
  const runs = [run, { ...run, task_id: "second-child", card_id: "second-card" }];
  const snapshot = { messages: [], dispatch_state: subagentDispatch, active_attempt: null, subagents: runs };
  applySessionSnapshot("s", structuredClone(snapshot));
  const first = useSessionStore.getState().subagents.s;
  expect(Object.keys(first)).toEqual(["card", "second-card"]);

  for (let read = 0; read < 5; read++) {
    applySessionSnapshot("s", structuredClone(snapshot));
    expect(useSessionStore.getState().subagents.s).toBe(first);
  }

  applySessionSnapshot("s", { ...snapshot, subagents: [...runs].reverse() });
  expect(Object.keys(useSessionStore.getState().subagents.s)).toEqual(["second-card", "card"]);
});

it("publishes a subagent's terminal status from a complete snapshot", () => {
  const run: SubagentRun = { task_id: "child", card_id: "card", status: "running", name: "Worker",
    description: "", prompt: "", summary: "", steps: [] };
  const { setSubagents } = useSessionStore.getState();
  setSubagents("s", [run], {});
  const running = useSessionStore.getState().subagents.s;

  const finished: SubagentRun = { ...run, status: "completed", summary: "Done" };
  setSubagents("s", [finished], running);
  const completed = useSessionStore.getState().subagents.s;
  expect(completed).not.toBe(running);
  expect(completed.card).toMatchObject({ status: "completed", summary: "Done" });
});

it("retains a same-value live observation when an older subagent snapshot arrives", () => {
  const live = { type: "subagent", session_id: "s", task_id: "child", tool_call_id: "call",
    status: "running", name: "Worker", description: "Observed progress" };
  const { setSubagents } = useSessionStore.getState();
  handleWsMessage(live);
  const readStart = useSessionStore.getState().subagents.s;
  handleWsMessage(live);
  const observed = useSessionStore.getState().subagents.s;
  expect(observed).toEqual(readStart);

  setSubagents("s", [structuredClone(observed.call)], observed);
  expect(useSessionStore.getState().subagents.s).toBe(observed);

  setSubagents("s", [{ ...readStart.call, description: "Earlier progress" }], readStart);
  const afterOldRead = useSessionStore.getState().subagents.s;
  expect(afterOldRead).toBe(observed);
  expect(afterOldRead.call.description).toBe("Observed progress");
});

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
  const dispatch = { generation: "cards", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] };
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
  store.upsertBgTask("s", { ...original, output_preview: "Full fetched output" }, original);
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({
    delivery_disposition: "unknown", delivery_error: "ACK lost", output_preview: "Full fetched output",
  });
  // A detail request begun after that observation may report its later
  // reconciliation. The old event alone must not freeze unknown forever.
  const detailStart = useSessionStore.getState().bgTasks.s[0];
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "bg", status: "completed", exit_code: 0, completed_at: "now" });
  store.upsertBgTask("s", { ...original, output_preview: "Later fetched output" }, detailStart);
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({
    delivery_disposition: null, delivery_error: null, output_preview: "Later fetched output",
  });
});

it("merges background and research snapshots using request-start entities", () => {
  const store = useSessionStore.getState();
  const bg = { id: "bg", status: "running" } as BgTask;
  const research = { id: "research", question: "q", status: "running" } as ResearchJob;
  store.setBgTasks("s", [bg]);
  store.setResearch("s", [research]);
  const before = useSessionStore.getState();
  store.upsertBgTask("s", { ...bg, status: "completed", output_preview: "new result" });
  handleWsMessage({ type: "research_completed", session_id: "s", research_id: "research", verified: 2 });
  store.setBgTasks("s", [bg], before.bgTasks.s);
  store.setResearch("s", [research], before.research.s);
  expect(useSessionStore.getState().bgTasks.s[0].output_preview).toBe("new result");
  expect(useSessionStore.getState().research.s[0].verified).toBe(2);
});
