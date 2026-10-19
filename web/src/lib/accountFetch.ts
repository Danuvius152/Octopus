import { useSessionStore } from "../stores/sessionStore";

/** An account response can never commit into a later browser identity. */
export async function accountFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const state = useSessionStore.getState();
  const generation = state.accountGeneration;
  const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
  const bearer = headers.get("Authorization");
  if (bearer && bearer !== `Bearer ${state.token}`.trimEnd()) {
    throw new DOMException("The request belongs to a previous sign-in", "AbortError");
  }
  const check = () => {
    if (useSessionStore.getState().accountGeneration !== generation) {
      throw new DOMException("The response belongs to a previous sign-in", "AbortError");
    }
  };
  const wrap = (response: Response): Response => new Proxy(response, {
    get(target, key) {
      const value = Reflect.get(target, key, target);
      if (key === "clone") return () => wrap(target.clone());
      if (["json", "text", "blob", "arrayBuffer", "formData", "bytes"].includes(String(key)) && typeof value === "function") {
        return async () => { const body = await value.call(target); check(); return body; };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const response = await globalThis.fetch(input, init);
  check();
  return wrap(response);
}
