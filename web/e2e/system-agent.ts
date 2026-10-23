import { expect, type APIRequestContext } from "@playwright/test";

/** Select this fixture account's existing agent before explicit creation. */
export async function systemAgentId(
  request: APIRequestContext,
  sessionEndpoint: string,
  headers: Record<string, string>,
): Promise<string> {
  const response = await request.get(new URL("agents", sessionEndpoint).href, { headers });
  expect(response.ok()).toBeTruthy();
  const agents = await response.json() as { id: string; is_system?: boolean }[];
  const agent = agents.find((row) => row.is_system);
  if (!agent) throw new Error("The current fixture account has no system agent");
  return agent.id;
}
