import { fileURLToPath } from "node:url";
import { expect, test } from "./test";

test("the development server rejects files outside the web roots, including the shared Vitest fixture", async ({ request, baseURL }) => {
  for (const path of ["../../tests/fixtures/wiki-command-cases.json", "../../pyproject.toml"]) {
    const file = fileURLToPath(new URL(path, import.meta.url));
    const response = await request.get(`${baseURL}/@fs${file}?raw`);
    expect(response.status()).toBe(403);
  }
});
