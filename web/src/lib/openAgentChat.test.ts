import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { openAgentChat } from "./openAgentChat";
import { sessions } from "./sessionList";

const created = { id: "created", agent_id: "agent", name: "Created", archived: false, status: "idle" } as SessionInfo;
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
let post: (response: Response) => void;
let reads: ((response: Response) => void)[];
beforeEach(() => {
  useSessionStore.getState().setToken(`account-${useSessionStore.getState().accountGeneration}`);
  useSessionStore.setState({ dispatchGeneration: "service" });
  reads = [];
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => init?.method === "POST"
    ? new Promise<Response>((resolve) => { post = resolve; })
    : new Promise<Response>((resolve) => { reads.push(resolve); })));
});
afterEach(() => { useSessionStore.getState().setToken(""); vi.unstubAllGlobals(); });

it("does not resurrect an unknown session archived before its creation receipt arrives", async () => {
  const creating = openAgentChat("agent", () => true);
  handleWsMessage({ type: "session_archived", old_session_id: created.id, new_session_id: null });
  post(Response.json(created));
  await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
  expect(reads).toHaveLength(1);
  reads[0](Response.json([])); await flush();
  // The sparse broadcast starts an inclusive read before POST completes.
  // Its snapshot can predate that receipt, so creation waits for the owner's
  // coalesced confirmation instead of trusting the first missing row.
  expect(reads).toHaveLength(2);
  const archived = { ...created, archived: true };
  reads[1](Response.json([archived]));
  await creating;
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([archived]);
  expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
});

it("confirms a successful create received after reconnect already read the old list", async () => {
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  sessions.connect(first); await flush(); reads[0](Response.json([])); await flush();
  const creating = openAgentChat("agent", () => true);
  sessions.disconnect(first); sessions.connect(second); await flush();
  reads[1](Response.json([])); await flush();
  post(Response.json(created)); await creating; await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(reads).toHaveLength(3);
  reads[2](Response.json([created])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([created]);
});

it.each(["token", "account"] as const)("does not navigate when the %s changes between confirmed creation and its consumer", async (scope) => {
  const foreign = { ...created, id: "foreign", agent_id: "foreign-agent" };
  const fetcher = vi.fn((url: string, init?: RequestInit) => init?.method === "POST"
    ? new Promise<Response>((resolve) => { post = resolve; })
    : Promise.resolve(Response.json(url.endsWith(`/${created.id}`) ? { ...created, messages: [] } : [])));
  vi.stubGlobal("fetch", fetcher);
  const unsubscribe = useSessionStore.subscribe((state) => {
    if (!state.sessions.some((row) => row.id === created.id)) return;
    unsubscribe();
    // Queue the change after the list adopts the receipt but before the
    // awaiting consumer resumes; the returned boolean was true in its scope.
    queueMicrotask(() => {
      if (scope === "token") useSessionStore.getState().setToken("foreign-token");
      else useSessionStore.setState({ accountGeneration: state.accountGeneration + 1 });
      useSessionStore.setState({ sessions: [foreign], activeSessionId: foreign.id, activeAgentId: foreign.agent_id });
    });
  });
  try {
    const creating = openAgentChat("agent", () => true);
    post(Response.json(created));
    await creating;
    expect(useSessionStore.getState().activeSessionId).toBe(foreign.id);
    expect(useSessionStore.getState().activeAgentId).toBe(foreign.agent_id);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method !== "POST")).toEqual([]);
  } finally {
    unsubscribe();
  }
});
