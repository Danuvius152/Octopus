import { expect, test } from "./test";
import { capturePaint } from "./paint-capture";

test("a failed warm-up releases capture and preserves the original failure", async ({ page }, info) => {
  const failure = new Error("Warm-up failed");
  const result = await capturePaint(page, info, async () => {
    await page.goto("/");
    throw failure;
  }).catch((error: unknown) => error);
  expect(result).toBeInstanceOf(AggregateError);
  expect((result as AggregateError).errors).toEqual([failure]);
  expect((result as AggregateError).message).toContain(failure.message);
  await page.goto("/");
  await expect(page.locator("body")).toBeVisible();
});

test("a closed capture target reports its errors through the current test", async ({ page }, info) => {
  const result = await capturePaint(page, info, async () => {
    await page.goto("/");
    await page.close();
  }).catch((error: unknown) => error);
  expect(result).toBeInstanceOf(AggregateError);
  expect((result as AggregateError).errors.length).toBeGreaterThan(0);
  expect((result as AggregateError).errors.every((error: unknown) => error instanceof Error)).toBe(true);
});

test("body and recorder failures survive cleanup and do not skip disposal", async ({ page }, info) => {
  const body = new Error("Body assertion failed");
  const stop = new Error("Recorder stop failed");
  let disposed = false;
  const result = await capturePaint(page, info, async (_frames, onCleanup) => {
    onCleanup(() => { throw stop; });
    onCleanup(() => { disposed = true; });
    await page.goto("/");
    throw body;
  }).catch((error: unknown) => error);
  expect(result).toBeInstanceOf(AggregateError);
  expect((result as AggregateError).errors).toEqual([body, stop]);
  expect((result as AggregateError).message).toContain(body.message);
  expect((result as AggregateError).message).toContain(stop.message);
  expect(disposed).toBe(true);
});
