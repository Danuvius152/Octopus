import { beforeEach, expect, it } from "vitest";
import { useSessionStore, type BgTask, type SubagentRun, type ResearchJob } from "../stores/sessionStore";
import { applySessionSnapshot } from "./hydrateSession";

beforeEach(() => useSessionStore.setState({ subagents: {}, bgTasks: {}, research: {} }));

it("retains a newer subagent result across an older detail response", () => {
  const store = useSessionStore.getState();
  const running = { tool_call_id: "call", task_id: "child", status: "running", name: "Worker" } as SubagentRun;
  store.upsertSubagent("s", running);
  const before = useSessionStore.getState();
  store.upsertSubagent("s", { ...running, status: "completed", summary: "new answer" });
  const dispatch = { generation: "cards", revision: 0, paused: false, active_request_id: null, items: [] };
  applySessionSnapshot("s", { messages: [], subagents: [running], dispatch_state: dispatch }, before);
  expect(useSessionStore.getState().subagents.s.call.summary).toBe("new answer");
  const nextStart = useSessionStore.getState();
  applySessionSnapshot("s", { messages: [], subagents: [{ ...running, summary: "new round" }], dispatch_state: dispatch }, nextStart);
  expect(useSessionStore.getState().subagents.s.call.status).toBe("running");
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
