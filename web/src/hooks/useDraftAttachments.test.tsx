import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useDraftAttachments } from "./useDraftAttachments";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("reports every file exceeding the limit, including subsequent additions", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  const { result } = renderHook(() => useDraftAttachments("s1"));
  const files = Array.from({ length: 11 }, (_, index) => new File(["evidence"], `file-${index}.txt`));
  act(() => result.current.addFiles(files));
  expect(result.current.attachments).toHaveLength(10);
  expect(result.current.rejectedFiles.map((file) => file.name)).toEqual(["file-10.txt"]);
  act(() => result.current.addFiles([new File(["x"], "extra.txt")]));
  expect(result.current.rejectedFiles.map((file) => file.name)).toEqual(["file-10.txt", "extra.txt"]);
  expect(result.current.rejectedFiles.every((file) => file.reason.includes("10"))).toBe(true);
});
