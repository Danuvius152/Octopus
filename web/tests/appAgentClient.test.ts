import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, it, vi } from "vitest";

const baseUrl = "http://localhost/apps/example/agent/";
const receipt = { code: "response_wait_timeout", conversation_id: "conversation", request_id: "original-request",
  operation_terminal: false, state: "running", status_url: "/apps/example/agent/conversations/conversation/requests/original-request" };

async function client(fetchImpl: typeof fetch, selectedBaseUrl = baseUrl) {
  const source = await readFile(resolve("../server/app_agent_client.js"), "utf8");
  const sdk = await import(/* @vite-ignore */ `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  return sdk.createAgentClient({ baseUrl: selectedBaseUrl, fetchImpl });
}

it("recovers a JSON response timeout with one GET of the original request and no second POST", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>()
    .mockResolvedValueOnce(Response.json({ detail: receipt }, { status: 504 }))
    .mockResolvedValueOnce(Response.json({ conversation_id: "conversation", request_id: "original-request", state: "completed", reply: "Original result" }));
  const sdk = await client(fetch);
  const result = await sdk.ask({ message: "Perform once" });
  expect(result.code).toBe("response_wait_timeout");
  expect(result.operation_terminal).toBe(false);
  expect(result.observation).toMatchObject({ state: "completed", request_id: "original-request", reply: "Original result" });
  expect(fetch.mock.calls.map(([url, options]) => [String(url), options?.method])).toEqual([
    [baseUrl + "ask", "POST"], ["http://localhost" + receipt.status_url, "GET"],
  ]);
});

it("closes a chunked SSE observation before querying the same request once", async () => {
  const order: string[] = [];
  const cancel = vi.fn(() => { order.push("reader closed"); });
  const wire = new TextEncoder().encode([
    { type: "conversation", conversation_id: "conversation", request_id: "original-request" },
    { type: "delta", request_id: "original-request", text: "中文分块" },
    { type: "error", ...receipt },
  ].map((frame) => `event: ${frame.type}\r\ndata: ${JSON.stringify(frame)}\r\n\r\n`).join(""));
  const stream = new ReadableStream({ start(controller) {
    for (let index = 0; index < wire.length; index += 3) controller.enqueue(wire.slice(index, index + 3));
  }, cancel });
  const fetch = vi.fn<typeof globalThis.fetch>(async (_url, options) => {
    order.push(options?.method ?? "unspecified");
    return options?.method === "POST" ? new Response(stream, { headers: { "Content-Type": "text/event-stream" } }) :
      Response.json({ conversation_id: "conversation", request_id: "original-request", state: "running", waiting_for_input: true });
  });
  const sdk = await client(fetch), onEvent = vi.fn();
  const result = await sdk.chat({ message: "Perform once" }, { onEvent });
  expect(result.code).toBe("response_wait_timeout");
  expect(result.observation).toMatchObject({ state: "running", waiting_for_input: true });
  expect(onEvent.mock.calls.map(([event]) => event.type)).toEqual(["conversation", "delta", "error"]);
  expect(onEvent.mock.calls[1][0].text).toBe("中文分块");
  expect(order).toEqual(["POST", "reader closed", "GET"]);
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[1][0].toString()).toBe("http://localhost" + receipt.status_url);
});

it.each(["network", "unknown", "invalid-reference", "identity-mismatch"])(
  "keeps the original response receipt when its status read is %s", async (failure) => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json({ detail: receipt }, { status: 504 }));
    if (failure === "network") fetch.mockRejectedValueOnce(new TypeError("Network unavailable"));
    else if (failure === "invalid-reference") fetch.mockResolvedValueOnce(Response.json({ detail: "Invalid answer reference" }, { status: 409 }));
    else fetch.mockResolvedValueOnce(Response.json({ conversation_id: "conversation",
      request_id: failure === "identity-mismatch" ? "another-request" : "original-request", state: "unknown" }));
    const sdk = await client(fetch);
    const result = await sdk.ask({ message: "Perform once" });
    expect(result).toMatchObject(receipt);
    if (failure === "unknown") expect(result.observation.state).toBe("unknown");
    else expect(result.observation_error).toEqual(expect.any(String));
    expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST", "GET"]);
  });

it("rejects a recovery URL outside the original app before attaching credentials", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json({
    detail: { ...receipt, status_url: "https://another-origin.example/steal" },
  }, { status: 504 }));
  const result = await (await client(fetch)).ask({ message: "Perform once" });
  expect(result).toMatchObject({ code: "response_wait_timeout", request_id: "original-request", operation_terminal: false });
  expect(result.observation_error).toMatch(/original application request/);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("keeps an interrupted stream's original identity without replaying it", async () => {
  const wire = `data: ${JSON.stringify({ type: "conversation", conversation_id: "conversation", request_id: "original-request" })}\n\n`;
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(wire));
  const sdk = await client(fetch);
  await expect(sdk.chat({ message: "Perform once" })).rejects.toMatchObject({ receipt: {
    conversation_id: "conversation", request_id: "original-request", operation_terminal: false,
  } });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each(["network-error", "abort", "frozen-abort"])("keeps the original request and exception after a stream %s", async (failure) => {
  const aborted = failure !== "network-error";
  const original = aborted ? new DOMException("Original observation aborted", "AbortError") : new TypeError("Original network error");
  if (failure === "frozen-abort") Object.freeze(original);
  const abort = new AbortController();
  const wire = `data: ${JSON.stringify({ type: "conversation", conversation_id: "conversation", request_id: "original-request" })}\n\n`;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(wire));
      abort.signal.addEventListener("abort", () => controller.error(abort.signal.reason));
    },
    pull(controller) {
      if (aborted) abort.abort(original);
      else controller.error(original);
    },
  });
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(stream));
  const sdk = await client(fetch);
  const error = await sdk.chat({ message: "Perform once" }, aborted ? { signal: abort.signal } : {}).catch((error: unknown) => error);
  if (failure === "frozen-abort") expect(error).toHaveProperty("cause", original);
  else expect(error).toBe(original);
  expect(error).toMatchObject({ receipt: { conversation_id: "conversation", request_id: "original-request", operation_terminal: false } });
  expect(stream.locked).toBe(false);
  expect(fetch).toHaveBeenCalledTimes(1);
  if (aborted) expect(fetch.mock.calls[0][1]?.signal).toBe(abort.signal);
});

it.each(["ask", "chat", "status"])("uses the product API base without a trailing slash for %s", async (operation) => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(operation === "chat" ? new Response(
    `data: ${JSON.stringify({ type: "conversation", conversation_id: "conversation", request_id: "original-request" })}\n\n` +
    `data: ${JSON.stringify({ type: "done", request_id: "original-request" })}\n\n`,
  ) : Response.json({ conversation_id: "conversation", request_id: "original-request", state: "completed", reply: "Original result" }));
  const sdk = await client(fetch, "http://localhost/apps/example/agent");
  if (operation === "status") await sdk.status(receipt);
  else await sdk[operation]({ message: "Perform once" });
  expect(fetch.mock.calls.map(([url, init]) => [String(url), init?.method])).toEqual([[
    operation === "status" ? "http://localhost/apps/example/agent/conversations/conversation/requests/original-request" :
      `http://localhost/apps/example/agent/${operation}`, operation === "status" ? "GET" : "POST",
  ]]);
});
