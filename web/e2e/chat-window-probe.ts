import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { ChatView } from "../src/components/ChatView";
import { useSessionStore, type Message, type SessionInfo } from "../src/stores/sessionStore";
import { applyTranscript } from "../src/lib/transcript";

/** Native RO, variable rows and the real presentation owner; no size-tree mocks. */
export function mountWindowProbe(count: number, footerHeight: number, tailMarkdown = "") {
  const session = { id: "window-probe", name: "Window probe", backend: "codex", status: "running",
    created_at: "2026-11-17", working_dir: "/tmp", archived: false } as SessionInfo;
  const rows = (first: number): Message[] => Array.from({ length: count }, (_, index) => ({
    role: "assistant", type: "text", seq: index ? index + 9 : first,
    content: `Saved row ${index}\n\n` + "A paragraph wrapping at the current font and viewport width. ".repeat(index % 7 + 1)
      + (index === count - 1 ? tailMarkdown : ""),
  }));
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: session.id, mainView: "chat", sessions: [session],
    archivedSessions: [], messages: {}, messageWindows: {}, oldestSeq: {}, lastAppliedSeq: {}, hasMoreMessages: {},
    agents: [], credentials: [], pendingQuestions: {}, subagents: {}, streamingItems: {},
    streamingText: {},
    dispatchStates: {}, dispatchUnavailable: {} });
  const all = rows(1);
  const recent = all.slice(-200);
  applyTranscript(session.id, { messages: recent, oldest_loaded_seq: recent[0].seq,
    next_message_seq: count + 9, has_more_messages: count > 200 });
  if (count > 200) {
    const state = useSessionStore.getState();
    state.prependMessages(session.id, all.slice(0, -200), 1, false,
      { window: state.messageWindows[session.id], beforeSeq: recent[0].seq! });
  }
  if (footerHeight) useSessionStore.getState().appendStreamingText(session.id, "Live output in a tall Footer");
  const container = document.createElement("div");
  container.className = "positioning-probe";
  container.style.cssText = "position:fixed;inset:0;display:flex;flex-direction:column;z-index:10000;background:hsl(var(--background))";
  const style = document.createElement("style");
  style.textContent = `.positioning-probe .msg-streaming { height: ${footerHeight}px; }`;
  container.append(style); document.body.append(container);
  const content = document.createElement("div");
  content.style.cssText = "display:flex;flex:1;min-height:0;flex-direction:column";
  container.append(content);
  const root = createRoot(content);
  let startedAt = performance.now();
  root.render(createElement(ChatView, { sendMessage: () => {}, interrupt: () => {}, answerQuestion: () => {},
    connected: true, onToggleSidebar: () => {}, onOpenSchedules: () => {} }));
  const geometry = () => {
    const viewport = container.querySelector<HTMLElement>(".chat-messages");
    const tail = container.querySelector<HTMLElement>(`[data-message-key="message:${count + 8}"]`);
    const tailBox = tail?.getBoundingClientRect();
    const viewportBox = viewport?.getBoundingClientRect();
    return { at: performance.now(), visible: !!viewport && getComputedStyle(viewport).visibility === "visible",
      gap: viewport ? viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop : null,
      tailMounted: !!tail,
      tailVisible: !!tailBox && !!viewportBox && tailBox.bottom > viewportBox.top && tailBox.top < viewportBox.bottom,
      footerHeight: container.querySelector("[data-chat-footer]")?.parentElement?.getBoundingClientRect().height ?? 0,
      incomplete: Boolean(container.querySelector("[data-chat-timeout]")) };
  };
  const samples: ReturnType<typeof geometry>[] = [];
  const writes: { method: string; value: number | undefined; at: number; visible: boolean; application: boolean }[] = [];
  const recordWrite = (element: Element, method: string, value: number | undefined) => {
    if (!container.contains(element) || !element.classList.contains("chat-messages")) return;
    writes.push({ method, value, at: performance.now(), visible: getComputedStyle(element).visibility === "visible",
      application: new Error().stack?.includes("ChatView.tsx") ?? false });
  };
  const scrollMethods = { scrollTo: Element.prototype.scrollTo, scrollBy: Element.prototype.scrollBy };
  for (const method of ["scrollTo", "scrollBy"] as const) {
    Element.prototype[method] = function (optionsOrX?: ScrollToOptions | number, y?: number) {
      recordWrite(this, method, typeof optionsOrX === "number" ? y : optionsOrX?.top);
      Reflect.apply(scrollMethods[method], this, typeof optionsOrX === "number" ? [optionsOrX, y ?? 0] : [optionsOrX ?? {}]);
    };
  }
  const scrollTop = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop")!;
  Object.defineProperty(Element.prototype, "scrollTop", { ...scrollTop, set(this: Element, value: number) {
    recordWrite(this, "scrollTop", value);
    scrollTop.set!.call(this, value);
  } });
  const longTasks: { at: number; duration: number }[] = [];
  const recordTasks = (entries: PerformanceEntry[]) => {
    for (const entry of entries) if (entry.startTime >= startedAt) longTasks.push({ at: entry.startTime, duration: entry.duration });
  };
  const tasks = new PerformanceObserver((list) => recordTasks(list.getEntries()));
  tasks.observe({ type: "longtask" });
  let frame = 0;
  const sample = () => { samples.push(geometry()); frame = requestAnimationFrame(sample); };
  sample();
  return {
    geometry,
    replace: (first: number) => {
      samples.length = 0; writes.length = 0; longTasks.length = 0; tasks.takeRecords();
      startedAt = performance.now();
      applyTranscript(session.id, { messages: rows(first), oldest_loaded_seq: first, next_message_seq: count + 9 });
      return startedAt;
    },
    append: (text: string) => useSessionStore.getState().appendStreamingText(session.id, text),
    report: () => {
      recordTasks(tasks.takeRecords());
      const firstVisible = samples.find((sample) => sample.visible);
      return { timeOrigin: performance.timeOrigin, startedAt, samples: [...samples], writes: [...writes], longTasks: [...longTasks],
        firstReadableMs: firstVisible ? firstVisible.at - startedAt : null,
        normalReady: Boolean(firstVisible && !firstVisible.incomplete),
        timeouts: samples.filter((sample, index) => sample.incomplete && !samples[index - 1]?.incomplete).length };
    },
    dispose: () => {
      cancelAnimationFrame(frame); tasks.disconnect();
      Object.assign(Element.prototype, scrollMethods);
      Object.defineProperty(Element.prototype, "scrollTop", scrollTop);
      root.unmount(); container.remove();
    },
  };
}
