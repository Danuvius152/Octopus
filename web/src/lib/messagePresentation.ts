/** Hide only bodies already displayed from their canonical message fields. */
export function displayToolInput(input: Record<string, unknown> | null | undefined, content: unknown) {
  if (!input) return input;
  const result = { ...input };
  const canonicalInput = Object.fromEntries(Object.entries(input).filter(([key]) => !["native_item", "request_context"].includes(key)));
  const same = (left: unknown, right: unknown) => right != null && JSON.stringify(left) === JSON.stringify(right);
  for (const source of ["native_item", "request_context"]) {
    const value = input[source];
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    result[source] = Object.fromEntries(Object.entries(value).filter(([key, body]) =>
      !(["text", "content", "output", "aggregatedOutput", "result"].includes(key) && same(body, content)) &&
      !(["arguments", "input"].includes(key) && same(body, canonicalInput))));
  }
  return result;
}
