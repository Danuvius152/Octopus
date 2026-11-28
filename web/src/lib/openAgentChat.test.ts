import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { openAgentChat } from "./openAgentChat";
import { sessions } from "./sessionList";

const created: SessionInfo = {
  id: "created", agent_id: "agent", name: "Created", archived: false, status: "idle",
  working_dir: "/tmp", created_at: "2026-01-01T00:00:00Z", message_count: 0, transcript_integrity: "ok", configuration_version: 0,
  origin: "user", backend: "codex", can_steer: false, can_observe_context: false, can_compact: false,
  can_fork: false, fork_is_full_copy: false,
};
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
let post: (response: Response) => void;
let reads: { url: string; complete: (response: Response) => void }[];
beforeEach(() => {
  useSessionStore.getState().setToken(`account-${useSessionStore.getState().accountGeneration}`);
  useSessionStore.setState({ dispatchGeneration: "service" });
  reads = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((resolve) => { post = resolve; });
    if (new URL(url).pathname === "/api/sessions") {
      return new Promise<Response>((complete) => { reads.push({ url, complete }); });
    }
    return Promise.resolve(Response.json([]));
  }));
});
afterEach(() => { useSessionStore.getState().setToken(""); vi.unstubAllGlobals(); });

it("does not resurrect an unknown session archived before its creation receipt arrives", async () => {
  const creating = openAgentChat("agent", () => true);
  await flush();
  handleWsMessage({ type: "session_archived", old_session_id: created.id, new_session_id: null });
  post(Response.json(created, { status: 201 }));
  await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
  expect(reads).toHaveLength(1);
  const archived = { ...created, archived: true };
  reads[0].complete(Response.json([archived])); await flush();
  await creating;
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([archived]);
  expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
});

it("confirms a successful create received after reconnect already read the old list", async () => {
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  sessions.connect(first); await flush(); reads[0].complete(Response.json([])); await flush();
  const creating = openAgentChat("agent", () => true);
  sessions.disconnect(first); sessions.connect(second); await flush();
  reads[1].complete(Response.json([])); await flush();
  post(Response.json(created, { status: 201 })); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(reads).toHaveLength(3);
  expect(reads[2].url).toContain("?include_archived=true");
  reads[2].complete(Response.json([created]));
  await creating;
  expect(useSessionStore.getState().sessions).toEqual([created]);
  expect(useSessionStore.getState().activeSessionId).toBe(created.id);
});

it("uses an accepted restore that follows an archive while waiting for the 201", async () => {
  const creating = openAgentChat("agent", () => true);
  await flush();
  handleWsMessage({ type: "session_archived", old_session_id: created.id, new_session_id: null });
  const restored = { ...created, name: "Renamed during archive" };
  handleWsMessage({ type: "session_unarchived", session_id: created.id, session: restored });
  post(Response.json(created, { status: 201 }));
  await creating;
  expect(useSessionStore.getState().sessions).toEqual([restored]);
  expect(useSessionStore.getState().activeSessionId).toBe(created.id);
  reads[0].complete(Response.json([])); await flush();
  reads[1].complete(Response.json([restored])); await flush();
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
    await flush();
    post(Response.json(created, { status: 201 }));
    await creating;
    expect(useSessionStore.getState().activeSessionId).toBe(foreign.id);
    expect(useSessionStore.getState().activeAgentId).toBe(foreign.agent_id);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method !== "POST")).toEqual([]);
  } finally {
    unsubscribe();
  }
});
