import { expect, test } from "@playwright/test";

test("current questions submit complete answers and keep answered and cancelled history after reload", async ({ page, request }) => {
  const api = "http://localhost:8765/api";
  const headers = { Authorization: "Bearer changeme" };
  const listed = await request.get(`${api}/agents`, { headers });
  expect(listed.ok()).toBeTruthy();
  const agents = await listed.json() as { id: string; name: string; archived?: boolean; pinned?: boolean }[];
  const agent = agents.find((row) => !row.archived && row.pinned !== false);
  if (!agent) throw new Error("The offline harness needs a visible active agent");
  const created = await request.post(`${api}/agents/${agent.id}/sessions`, {
    headers, data: { name: `Current questions ${Date.now()}` },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const sent: Record<string, unknown>[] = [];
  const received: Record<string, unknown>[] = [];
  // Observe the product socket without intercepting its connection or frames.
  page.on("websocket", (socket) => {
    socket.on("framesent", (frame) => sent.push(JSON.parse(frame.payload.toString())));
    socket.on("framereceived", (frame) => received.push(JSON.parse(frame.payload.toString())));
  });
  const answers = () => sent.filter((frame) => frame.type === "answer_question" && frame.session_id === session.id);
  const openSession = async () => {
    const row = page.locator(".session-item", { hasText: session.name });
    if (!(await row.isVisible())) {
      await page.locator(".agent-item").filter({ has: page.getByText(agent.name, { exact: true }) }).click();
    }
    await row.click();
    await expect(page.locator(".chat-header .crumb-current")).toHaveText(session.name);
    await expect(page.locator(".conn-status.on")).toBeVisible();
  };
  const form = page.locator(".msg-question:not(.msg-question-done)");
  const answerText = "Q: Which destination?\nA: Harbor\n\nQ: Which transport?\nA: Train";
  const closedText = "Question closed: cancelled. No answer was received.";

  try {
    await page.goto("/");
    await page.locator('input[type="password"]').fill("changeme");
    await page.locator(".btn-login").click();
    await openSession();
    const asked = await request.post(`${api}/sessions/${session.id}/questions`, {
      headers,
      data: { questions: [
        { header: "Destination", question: "Which destination?", options: [{ label: "Harbor" }, { label: "Forest" }] },
        { header: "Transport", question: "Which transport?", options: [{ label: "Train" }, { label: "Ferry" }] },
      ] },
    });
    expect(asked.status()).toBe(201);
    const question = await asked.json();
    await expect(form).toBeVisible();
    await expect(form.locator(".question-text")).toHaveText(["Which destination?", "Which transport?"]);
    await expect.poll(() => received.filter((frame) => frame.type === "question_request" && frame.question_id === question.question_id).length).toBe(1);
    const submit = form.getByRole("button", { name: "Submit", exact: true });
    await expect(submit).toBeDisabled();
    await form.getByRole("radio", { name: "Harbor", exact: true }).check();
    await expect(submit).toBeDisabled();
    expect(answers()).toHaveLength(0);
    await form.getByRole("radio", { name: "Train", exact: true }).check();
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(form).toHaveCount(0);
    await expect(page.locator(".msg-question-answer")).toHaveText(answerText);
    await expect.poll(() => answers().length).toBe(1);
    expect(answers()[0]).toMatchObject({
      question_id: question.question_id, request_id: null, attempt_id: null,
      client_command_id: expect.any(String),
      answers: [{ selected: ["Harbor"] }, { selected: ["Train"] }],
    });
    expect(answers()[0].client_command_id).not.toBe("");
    const answered = await request.get(`${api}/sessions/${session.id}/questions/${question.question_id}/answer?timeout=1`, { headers });
    expect(answered.ok()).toBeTruthy();
    expect(await answered.json()).toEqual({ answer: answerText });

    const abandoned = await request.post(`${api}/sessions/${session.id}/questions`, {
      headers,
      data: { questions: [{ question: "Keep this abandoned question open?", options: [{ label: "Keep" }, { label: "Close" }] }] },
    });
    expect(abandoned.status()).toBe(201);
    const cancelled = await abandoned.json();
    await expect(form).toContainText("Keep this abandoned question open?");
    const closed = await request.delete(`${api}/sessions/${session.id}/questions/${cancelled.question_id}`, { headers });
    expect(closed.ok()).toBeTruthy();
    await expect(form).toHaveCount(0);
    await expect(page.locator(".msg-question-closed")).toHaveText(closedText);

    await page.reload();
    await openSession();
    await expect(page.locator(".msg-question-done .question-text")).toHaveText([
      "Which destination?", "Which transport?", "Keep this abandoned question open?",
    ]);
    await expect(page.locator(".msg-question-answer")).toHaveText(answerText);
    await expect(page.locator(".msg-question-closed")).toHaveText(closedText);
    await expect(form).toHaveCount(0);
    expect(answers()).toHaveLength(1);
    expect(sent.filter((frame) => frame.session_id === session.id).map((frame) => frame.type)).toEqual(["answer_question"]);
    const current = await request.get(`${api}/sessions/${session.id}`, { headers });
    expect(current.ok()).toBeTruthy();
    const detail = await current.json();
    expect(detail.status).toBe("idle");
    expect(detail.dispatch_state.active_request_id).toBeNull();
    expect(detail.pending_questions).toEqual([]);
    expect(detail.messages.map((message: { type: string }) => message.type)).toEqual([
      "question_request", "question_answer", "question_request", "question_closed",
    ]);
  } finally {
    const deleted = await request.delete(`${api}/sessions/${session.id}`, { headers });
    expect(deleted.ok()).toBeTruthy();
  }
});
