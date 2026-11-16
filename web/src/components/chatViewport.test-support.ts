import { vi } from "vitest";

// VirtuosoMockContext supplies item measurements; jsdom still reports the
// actual scroller as zero-height, so native initial scrolling ignores it.
export function mockChatViewport() {
  // JSDOM has no layout/ResizeObserver. These tests use VirtuosoMockContext;
  // actual resize and follow behavior is verified by the browser regressions.
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  const height = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight")!.get!;
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : height.call(this);
  });
}

// Real (variable-size) Virtuoso calls itemSize only when its browser measurement
// path is enabled. Supply dimensions for its public DOM items without claiming
// fixed sizes through VirtuosoMockContext; callers deliver ResizeObserver.
export function mockChatItemLayout() {
  const computed = window.getComputedStyle.bind(window);
  vi.stubGlobal("getComputedStyle", (element: Element) => {
    const style = computed(element);
    // Browsers resolve the initial gap to normal; jsdom leaves it empty.
    if (!style.rowGap) style.rowGap = "normal";
    if (!style.columnGap) style.columnGap = "normal";
    return style;
  });
  vi.spyOn(HTMLElement.prototype, "offsetParent", "get").mockImplementation(function (this: HTMLElement) {
    return this.parentElement;
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.closest(".chat-messages") ? 800 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const viewport = this.closest<HTMLElement>(".chat-messages");
    if (viewport && (this === viewport || this.hasAttribute("data-viewport-type"))) return new DOMRect(0, 0, 800, viewport.clientHeight);
    if (viewport && this.hasAttribute("data-index")) return new DOMRect(0, Number(this.dataset.index) * 80 - viewport.scrollTop, 800, 80);
    return new DOMRect();
  });
}
