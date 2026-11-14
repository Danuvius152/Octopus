import path from "node:path";
import { ESLint } from "eslint";
import { expect, it } from "vitest";

const eslint = new ESLint({ cwd: path.resolve(import.meta.dirname, "..") });
for (const code of [
  "globalThis.crypto.randomUUID()",
  "crypto.randomUUID()",
  "const c = globalThis.crypto; c.randomUUID()",
  'crypto["randomUUID"]()',
  "const { randomUUID: uuid } = crypto; uuid()",
]) {
  it(`rejects production bypass: ${code}`, async () => {
    const [result] = await eslint.lintText(code, { filePath: "src/lib/lint-probe.ts" });
    expect(result.messages.map((message) => message.ruleId)).toContain("no-restricted-properties");
  });
}

it("allows the helper consumer and the explicit helper/test exceptions", async () => {
  for (const [filePath, code] of [
    ["src/lib/lint-probe.ts", 'import { newSubmissionId } from "./submission"; newSubmissionId()'],
    ["src/lib/submission.ts", "crypto.randomUUID()"],
    ["src/lib/submission.test.ts", "crypto.randomUUID()"],
    ["src/test-support/id.ts", "crypto.randomUUID()"],
    ["e2e/id.spec.ts", "crypto.randomUUID()"],
  ]) {
    const [result] = await eslint.lintText(code, { filePath });
    expect(result.messages).toEqual([]);
  }
});
