import { afterEach, expect, it, vi } from "vitest";
import { newSubmissionId } from "./submission";

afterEach(() => vi.unstubAllGlobals());

it("refuses to create a submission identity without secure randomness", () => {
  vi.stubGlobal("crypto", undefined);
  expect(() => newSubmissionId()).toThrow(/secure|HTTPS/i);
});

it("uses secure random bytes when randomUUID is unavailable", () => {
  vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(0) });
  expect(newSubmissionId()).toBe("00000000-0000-4000-8000-000000000000");
});
