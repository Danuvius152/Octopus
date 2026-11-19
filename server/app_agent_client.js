/** App-scoped HTTP client. Response expiry observes the original request once. */
export function createAgentClient({ baseUrl, fetchImpl = globalThis.fetch.bind(globalThis), headers = {} } = {}) {
  const base = new URL(baseUrl ?? "./", import.meta.url);
  if (!base.pathname.endsWith("/")) base.pathname += "/";
  const request = (url, init) => {
    const selectedHeaders = new Headers(headers);
    new Headers(init.headers).forEach((value, key) => selectedHeaders.set(key, value));
    return fetchImpl(url, { credentials: "same-origin", ...init, headers: selectedHeaders });
  };

  function validId(id) {
    return typeof id === "string" && /^[a-zA-Z0-9_-]+$/.test(id);
  }

  function identityOf({ conversation_id, request_id }) {
    if (validId(conversation_id) && validId(request_id)) return { conversation_id, request_id };
  }

  function requestRoute(pathname) {
    return pathname.match(/\/apps\/[^/]+\/agent\/conversations\/[^/]+\/requests\/[^/]+$/)?.[0] ?? pathname;
  }

  function statusUrl(receipt) {
    if (!identityOf(receipt)) {
      throw new Error("The original request identity is unavailable. Do not repeat the operation automatically.");
    }
    const expected = new URL(`conversations/${encodeURIComponent(receipt.conversation_id)}/requests/${encodeURIComponent(receipt.request_id)}`, base);
    if (receipt.status_url) {
      const offered = new URL(receipt.status_url, base);
      // The request route binds the identity. The POST base owns the mount
      // prefix, which can differ between a browser and the backend loopback.
      if (offered.origin !== expected.origin || offered.username || offered.password || offered.search || offered.hash ||
          requestRoute(offered.pathname) !== requestRoute(expected.pathname)) {
        throw new Error("The recovery URL does not identify the original application request.");
      }
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

  function observationFailure(error, identity) {
    // AbortSignal can reject with a caller-supplied immutable or non-Error reason.
    const failure = error && (typeof error === "object" || typeof error === "function") && Object.isExtensible(error)
      ? error : new Error("The response stream failed.", { cause: error });
    return Object.assign(failure, { receipt: { ...identity, operation_terminal: false } });
  }

  async function readChat(response, onEvent) {
    const headerFields = {
      conversation_id: response.headers.get("X-Octopus-Conversation-Id"),
      request_id: response.headers.get("X-Octopus-Request-Id"),
    };
    let identity = identityOf(headerFields);
    const decoder = new TextDecoder();
    let buffer = "", data = [], firstEvent = true, reader, terminal;
    try {
      if (!response.body) throw new Error("The response has no event stream.");
      reader = response.body.getReader();
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
            if (firstEvent && event.type === "conversation") {
              for (const key of ["conversation_id", "request_id"]) {
                if (validId(headerFields[key]) && validId(event[key]) && headerFields[key] !== event[key]) {
                  identity = undefined;
                  throw Object.assign(new Error("The event stream changed its original request identity."), {
                    code: "sse_protocol_error",
                  });
                }
              }
              const firstIdentity = identityOf(event);
              if (!firstIdentity) throw new Error("The first frame has no valid original request identity.");
              identity ??= firstIdentity;
            }
            firstEvent = false;
            if (identity) {
              try {
                if ((event.request_id != null && event.request_id !== identity.request_id) ||
                    (event.conversation_id != null && event.conversation_id !== identity.conversation_id)) {
                  throw new Error("The event stream changed its original request identity.");
                }
                if (event.status_url != null) statusUrl({ ...identity, status_url: event.status_url });
              } catch (error) {
                identity = undefined;
                throw Object.assign(error, { code: "sse_protocol_error" });
              }
            }
            if (event.type === "done" || event.type === "error") {
              if (!identity) throw new Error("The event stream has no valid original request identity.");
              terminal = { ...event, ...identity };
            }
            if (onEvent) await onEvent(event);
            if (terminal) return terminal;
          }
        }
        if (done) throw new Error("The response ended without a terminal event. Check the original request before sending again.");
      }
    } catch (error) {
      throw observationFailure(error, identity);
    } finally {
      try { await reader?.cancel(); }
      catch (error) {
        if (terminal) terminal.reader_error = error instanceof Error ? error.message : String(error);
      } finally { reader?.releaseLock(); }
    }
  }

  async function chat(input, options = {}) {
    let response;
    try {
      response = await request(new URL("chat", base), { method: "POST", signal: options.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
    } catch (error) {
      throw observationFailure(error);
    }
    if (!response.ok) throw responseError(response, await response.json());
    const terminal = await readChat(response, options.onEvent);
    return terminal.code === "response_wait_timeout" ? recover(terminal, options) : terminal;
  }

  return { ask, chat, status };
}
