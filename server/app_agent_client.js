/** App-scoped HTTP client. Response expiry observes the original request once. */
export function createAgentClient({ baseUrl, fetchImpl = globalThis.fetch.bind(globalThis), headers = {} } = {}) {
  const base = new URL(baseUrl ?? "./", import.meta.url);
  if (!base.pathname.endsWith("/")) base.pathname += "/";
  const request = (url, init) => {
    const selectedHeaders = new Headers(headers);
    new Headers(init.headers).forEach((value, key) => selectedHeaders.set(key, value));
    return fetchImpl(url, { credentials: "same-origin", ...init, headers: selectedHeaders });
  };

  function statusUrl(receipt) {
    if (typeof receipt.conversation_id !== "string" || !receipt.conversation_id ||
        typeof receipt.request_id !== "string" || !receipt.request_id) {
      throw new Error("The original request identity is unavailable. Do not repeat the operation automatically.");
    }
    const expected = new URL(`conversations/${encodeURIComponent(receipt.conversation_id)}/requests/${encodeURIComponent(receipt.request_id)}`, base);
    if (receipt.status_url && new URL(receipt.status_url, base).href !== expected.href) {
      throw new Error("The recovery URL does not identify the original application request.");
    }
    return expected;
  }

  function responseError(response, body) {
    const detail = body?.detail ?? body;
    return Object.assign(new Error(typeof detail === "string" ? detail : detail?.message ?? `Request failed (${response.status})`),
      { status: response.status, detail });
  }

  async function status(receipt, { signal } = {}) {
    const response = await request(statusUrl(receipt), { method: "GET", signal });
    const body = await response.json();
    if (!response.ok) throw responseError(response, body);
    if (body.conversation_id !== receipt.conversation_id || body.request_id !== receipt.request_id) {
      throw new Error("Status did not confirm the original request identity.");
    }
    return body;
  }

  async function recover(receipt, { signal } = {}) {
    try {
      const querySignal = AbortSignal.any([AbortSignal.timeout(10_000), ...(signal ? [signal] : [])]);
      return { ...receipt, observation: await status(receipt, { signal: querySignal }) };
    } catch (error) {
      // The original wait receipt remains usable when this one read fails.
      return { ...receipt, observation_error: error instanceof Error ? error.message : String(error) };
    }
  }

  async function ask(input, options = {}) {
    const response = await request(new URL("ask", base), { method: "POST", signal: options.signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
    const body = await response.json();
    if (response.status === 504 && body?.detail?.code === "response_wait_timeout") {
      return recover({ type: "error", ...body.detail }, options);
    }
    if (!response.ok) throw responseError(response, body);
    return body;
  }

  async function readChat(response, onEvent) {
    if (!response.body) throw new Error("The response has no event stream.");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "", data = [], identity = {}, terminal;
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        while (true) {
          const boundary = /\r\n|\r|\n/.exec(buffer);
          if (!boundary || !done && boundary[0] === "\r" && boundary.index === buffer.length - 1) break;
          const line = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          else if (line === "" && data.length) {
            const event = JSON.parse(data.join("\n"));
            data = [];
            if (!event || typeof event !== "object" || typeof event.type !== "string") {
              throw new Error("The event stream returned an invalid event.");
            }
            if (event.type === "conversation") {
              statusUrl(event); // Validate the original identity, without an HTTP request.
              identity = { conversation_id: event.conversation_id, request_id: event.request_id };
            } else if (identity.request_id && event.request_id && event.request_id !== identity.request_id) {
              throw new Error("The event stream changed its original request identity.");
            }
            if (event.type === "done" || event.type === "error") terminal = { ...identity, ...event };
            if (onEvent) await onEvent(event);
            if (terminal) return terminal;
          }
        }
        if (done) throw new Error("The response ended without a terminal event. Check the original request before sending again.");
      }
    } catch (error) {
      // AbortSignal can reject with a caller-supplied immutable or non-Error
      // reason. Keep that original cause when it cannot carry the receipt.
      const failure = error && (typeof error === "object" || typeof error === "function") && Object.isExtensible(error)
        ? error : new Error("The response stream failed.", { cause: error });
      throw Object.assign(failure, { receipt: { ...identity, operation_terminal: false } });
    } finally {
      try { await reader.cancel(); }
      catch (error) {
        if (terminal) terminal.reader_error = error instanceof Error ? error.message : String(error);
      } finally { reader.releaseLock(); }
    }
  }

  async function chat(input, options = {}) {
    const response = await request(new URL("chat", base), { method: "POST", signal: options.signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
    if (!response.ok) throw responseError(response, await response.json());
    const terminal = await readChat(response, options.onEvent);
    return terminal.code === "response_wait_timeout" ? recover(terminal, options) : terminal;
  }

  return { ask, chat, status };
}
