import { beforeEach, expect, it } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import replay from "./recordingReplay.json";

// The backend regression compares these projections with real get_session
// snapshots and the recovered broadcast, including a lost commit acknowledgement.
const session = replay.event.session_id;
beforeEach(() => useSessionStore.setState({ messages: {}, lastAppliedSeq: {},
  oldestSeq: {}, hasMoreMessages: {}, streamingText: {} }));

it.each(["before-save", "lost-ack", "broadcast-before-snapshot"])(
  "renders the recovered terminal once across %s", (mode) => {
    if (mode === "broadcast-before-snapshot") handleWsMessage(replay.event);
    applyTranscript(session, mode === "lost-ack" ? replay.after : replay.before);
    handleWsMessage(replay.event);
    handleWsMessage(replay.event);
    expect(useSessionStore.getState().messages[session]).toHaveLength(1);
    applyTranscript(session, replay.after);
    handleWsMessage(replay.event);
    const messages = useSessionStore.getState().messages[session];
    expect(messages).toHaveLength(1);
    expect(messages[0].seq).toBe(0);
    expect(messages[0].outcome).toMatchObject({ status: "success", request_id: "recording-replay" });
  }
);
