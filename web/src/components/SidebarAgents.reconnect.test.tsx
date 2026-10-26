import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SidebarAgents } from "./SidebarAgents";
import { useWebSocket } from "../hooks/useWebSocket";
import { useSessionStore, type Agent } from "../stores/sessionStore";

class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { Socket.instances.push(this); }
  close() { this.readyState = 3; }
}
const agent = (id: string, name: string): Agent => ({ id, name, pinned: true, archived: false, backend: "codex" } as Agent);
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("reconnect updates the rendered sidebar with offline creation, rename and archive", async () => {
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  let rows = [agent("a", "Old A"), agent("b", "Old B")];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/api/agents")) return Response.json(rows);
    if (url.endsWith("/api/backends")) return Response.json({ available: ["codex"] });
    return Response.json([]);
  }));
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token", accountGeneration: 1, dispatchGeneration: "service-a" });
  const socket = renderHook(() => useWebSocket());
  render(<SidebarAgents />);
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  expect(screen.getByText("Old A")).toBeVisible();
  act(() => {
    Socket.instances[0].readyState = 3;
    Socket.instances[0].onclose?.({ code: 1013 });
    rows = [agent("b", "Offline rename"), agent("c", "Offline addition")];
    vi.advanceTimersByTime(3000);
  });
  expect(Socket.instances).toHaveLength(2);
  await act(async () => { Socket.instances[1].readyState = 1; Socket.instances[1].onopen?.(); });
  vi.useRealTimers();
  await waitFor(() => expect(screen.getByText("Offline rename")).toBeVisible());
  expect(screen.getByText("Offline addition")).toBeVisible();
  expect(screen.queryByText("Old A")).toBeNull();
  expect(screen.queryByText("Old B")).toBeNull();
  socket.unmount();
});
