import { Socket } from "../test-support/socket";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { useDraftAttachments } from "./useDraftAttachments";
import { useWebSocket } from "./useWebSocket";


beforeEach(() => {
  Socket.reset(1);
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.getState().setToken("identity-test");
  useSessionStore.setState({ controlCommands: {}, pendingQuestions: {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps existing files and accepts another attachment without Web Crypto", async () => {
  const upload = vi.fn(async () => Response.json({ id: "uploaded", filename: "evidence.txt", size: 4, mime_type: "text/plain" }));
  vi.stubGlobal("fetch", upload);
  const { result } = renderHook(() => useDraftAttachments("s"));
  act(() => result.current.addFiles([new File(["first"], "first.txt")]));
  await waitFor(() => expect(result.current.attachments[0]?.status).toBe("ready"));
  vi.stubGlobal("crypto", undefined);
  act(() => result.current.addFiles([new File(["more"], "evidence.txt")]));
  await waitFor(() => expect(result.current.attachments).toHaveLength(2));
  await waitFor(() => expect(result.current.attachments[1]?.status).toBe("ready"));
  expect(result.current.attachments.map((file) => file.file.name)).toEqual(["first.txt", "evidence.txt"]);
  expect(new Set(result.current.attachments.map((file) => file.uid)).size).toBe(2);
});

it.each(["Stop", "answer"])("shows a failed %s notice when secure command identity is unavailable", async (operation) => {
  const { result } = renderHook(useWebSocket);
  vi.stubGlobal("crypto", undefined);
  let accepted = true;
  await act(async () => {
    const reply = await (operation === "Stop" ? result.current.interrupt("s") : result.current.answerQuestion("s", "question", [{ text: "Keep my answer" }]));
    accepted = reply.accepted;
  });
  expect(accepted).toBe(false);
  expect(Socket.instances[0].send).not.toHaveBeenCalled();
  expect(Object.values(useSessionStore.getState().controlCommands)).toEqual([
    expect.objectContaining({ sessionId: "s", state: "failed", message: expect.stringMatching(/secure randomness/i) }),
  ]);
});

it("returns an explicit refusal for programmatic message submission without secure randomness", async () => {
  const { result } = renderHook(useWebSocket);
  vi.stubGlobal("crypto", undefined);
  const reply = await result.current.sendMessage("s", "Retain this input");
  expect(reply).toMatchObject({ accepted: false, message: expect.stringMatching(/secure randomness/i) });
  expect(Socket.instances[0].send).not.toHaveBeenCalled();
});
