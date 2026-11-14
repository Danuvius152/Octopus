import { expect, type APIRequestContext } from "./test";

/** One explicit restore, never an automatic replay after an unknown reply. */
export async function restoreSession(request: APIRequestContext, api: string, id: string, headers: { [key: string]: string }) {
  const path = `${api}/sessions/${id}`;
  const status = await request.get(`${path}/restoration`, { headers });
  expect(status.ok(), await status.text()).toBeTruthy();
  const reply = await request.post(`${path}/unarchive`, { headers, data: {
    operation_id: globalThis.crypto.randomUUID(), service_generation: (await status.json()).service_generation,
  } });
  expect(reply.ok(), await reply.text()).toBeTruthy();
  expect(await reply.json()).toMatchObject({ state: "finished", status_code: null });
  return reply;
}
