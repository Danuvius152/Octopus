/** Human composer intent. `off` revokes access even if the draft says /wiki. */
export type WikiMessageMode = "read" | "maintain" | "off";

// Match Python str.split() at server/wiki/tasks.py's trusted input boundary.
// JS \s misses NEL and the information separators, and includes BOM instead.
// eslint-disable-next-line no-control-regex -- Python whitespace includes these control characters.
const PYTHON_WHITESPACE = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;

export function wikiCommandMode(input: string): "read" | "maintain" | undefined {
  // ChatView sends input.trim(); inspect exactly the text sent over the wire.
  const [command, mode] = input.trim().split(PYTHON_WHITESPACE).filter(Boolean);
  if (command !== "/wiki") return undefined;
  return mode === "maintain" ? "maintain" : "read";
}
