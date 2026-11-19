import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, it, vi } from "vitest";

const baseUrl = "http://localhost/apps/example/agent/";
const receipt = { code: "response_wait_timeout", conversation_id: "conversation", request_id: "original-request",
  operation_terminal: false, state: "running", status_url: "/apps/example/agent/conversations/conversation/requests/original-request" };
const identity = { conversation_id: receipt.conversation_id, request_id: receipt.request_id };
const identityHeaders = { "X-Octopus-Conversation-Id": receipt.conversation_id, "X-Octopus-Request-Id": receipt.request_id };
const firstFrame = { type: "conversation", ...identity };
const frame = (event: object) => `data: ${JSON.stringify(event)}\n\n`;

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

it.each([
  ["chat", "/octopus+local", "/octopus%2Blocal"],
  ["chat", "", "/octopus"],
  ["ask", "", "/octopus"],
  ["chat", "/octopus", ""],
] as const)("queries the original %s base %s when the receipt advertises %s", async (operation, basePrefix, receiptPrefix) => {
  const base = `http://localhost${basePrefix}/apps/example/agent/`;
  const offered = { ...receipt, status_url: receiptPrefix + receipt.status_url };
  const fetch = vi.fn<typeof globalThis.fetch>()
    .mockResolvedValueOnce(operation === "ask" ? Response.json({ detail: offered }, { status: 504 }) :
      new Response(frame(firstFrame) + frame({ type: "error", ...offered }), { headers: identityHeaders }))
    .mockResolvedValueOnce(Response.json({ ...identity, state: "completed" }));
  const sdk = await client(fetch, base);
  await expect(sdk[operation]({ message: "Perform once" })).resolves.toMatchObject({
    ...offered, observation: { ...identity, state: "completed" },
  });
  expect(fetch.mock.calls.map(([url, options]) => [String(url), options?.method])).toEqual([
    [base + operation, "POST"], ["http://localhost" + basePrefix + receipt.status_url, "GET"],
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

it.each([
  `https://another-origin.example${receipt.status_url}`,
  `/octopus${receipt.status_url.replace("/example/", "/another-app/")}`,
  `/octopus${receipt.status_url.replace("/conversations/conversation/", "/conversations/another-conversation/")}`,
  `/octopus${receipt.status_url.replace("/original-request", "/another-request")}`,
  `/octopus${receipt.status_url}?unexpected=1`,
])("rejects a conflicting recovery URL before querying status (%s)", async (status_url) => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json({
    detail: { ...receipt, status_url },
  }, { status: 504 }));
  const result = await (await client(fetch)).ask({ message: "Perform once" });
  expect(result).toMatchObject({ code: "response_wait_timeout", request_id: "original-request", operation_terminal: false });
  expect(result.observation_error).toMatch(/original application request/);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each(["identity headers and no first frame", "only a complete first frame", "both identity channels"])(
  "can read the original status after disconnect with %s", async (channel) => {
  const wire = channel === "identity headers and no first frame" ? null : frame(firstFrame);
  const headers = channel === "only a complete first frame" ? {} : identityHeaders;
  const fetch = vi.fn<typeof globalThis.fetch>()
    .mockResolvedValueOnce(new Response(wire, { headers }))
    .mockResolvedValueOnce(Response.json({ ...identity, state: "completed" }));
  const sdk = await client(fetch);
  const error = await sdk.chat({ message: "Perform once" }).catch((failure: unknown) => failure);
  expect(error).toMatchObject({ receipt: { ...identity, operation_terminal: false } });
  await expect(sdk.status((error as { receipt: object }).receipt)).resolves.toMatchObject({ state: "completed" });
  expect(fetch.mock.calls.map(([url, options]) => [String(url), options?.method])).toEqual([
    [baseUrl + "chat", "POST"], ["http://localhost" + receipt.status_url, "GET"],
  ]);
});

it.each(["incomplete first frame", "incomplete response headers", "transport failed before headers"])(
  "keeps the original operation unknown when identity is unavailable (%s)", async (channel) => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    if (channel === "transport failed before headers") fetch.mockRejectedValueOnce(new TypeError("Connection closed"));
    else if (channel === "incomplete response headers") {
      fetch.mockResolvedValueOnce(new Response(null, { headers: { "X-Octopus-Request-Id": identity.request_id } }));
    } else fetch.mockResolvedValueOnce(new Response(frame(firstFrame).slice(0, -1)));
    const sdk = await client(fetch);
    const error = await sdk.chat({ message: "Perform once" }).catch((failure: unknown) => failure);
    expect(error).toHaveProperty("receipt", { operation_terminal: false });
    await expect(sdk.status((error as { receipt: object }).receipt)).rejects.toThrow(/identity is unavailable/);
    expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST"]);
  },
);

it.each([
  ["conversation_id", { ...firstFrame, conversation_id: "another-conversation" }],
  ["request_id", { ...firstFrame, request_id: "another-request" }],
])("rejects conflicting response header and first-frame %s", async (_field, offered) => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(frame(offered), { headers: identityHeaders }));
  const sdk = await client(fetch), onEvent = vi.fn();
  const error = await sdk.chat({ message: "Perform once" }, { onEvent }).catch((failure: unknown) => failure);
  expect(error).toMatchObject({ code: "sse_protocol_error", receipt: { operation_terminal: false } });
  await expect(sdk.status((error as { receipt: object }).receipt)).rejects.toThrow(/identity is unavailable/);
  expect(onEvent).not.toHaveBeenCalled();
  expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST"]);
});

it.each([
  ["malformed first frame", { ...identityHeaders }, { ...firstFrame, conversation_id: "another-conversation", request_id: "not/a/request" }],
  ["incomplete response headers", { "X-Octopus-Conversation-Id": "another-conversation" }, firstFrame],
] as const)("rejects a valid conflicting field from %s", async (_source, headers, offered) => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(frame(offered), { headers }));
  const sdk = await client(fetch), onEvent = vi.fn();
  const error = await sdk.chat({ message: "Perform once" }, { onEvent }).catch((failure: unknown) => failure);
  expect(error).toHaveProperty("code", "sse_protocol_error");
  expect(error).toHaveProperty("receipt", { operation_terminal: false });
  await expect(sdk.status((error as { receipt: object }).receipt)).rejects.toThrow(/identity is unavailable/);
  expect(onEvent).not.toHaveBeenCalled();
  expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST"]);
});

it("keeps a complete response-header identity if the first frame is invalid but has no conflicting field", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(
    frame({ ...firstFrame, request_id: "not/a/request" }), { headers: identityHeaders },
  ));
  const sdk = await client(fetch);
  await expect(sdk.chat({ message: "Perform once" })).rejects.toHaveProperty("receipt", {
    ...identity, operation_terminal: false,
  });
  expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST"]);
});

it.each([
  { ...receipt, conversation_id: "another-conversation" },
  { ...receipt, status_url: "/apps/example/agent/conversations/conversation/requests/another-request" },
])("rejects a timeout frame that changes the confirmed response identity or URL", async (offered) => {
  const wire = frame(firstFrame) + frame({ type: "error", ...offered });
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(wire, { headers: identityHeaders }));
  const sdk = await client(fetch);
  const error = await sdk.chat({ message: "Perform once" }).catch((failure: unknown) => failure);
  expect(error).toHaveProperty("code", "sse_protocol_error");
  expect(error).toHaveProperty("receipt", { operation_terminal: false });
  expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(["POST"]);
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
