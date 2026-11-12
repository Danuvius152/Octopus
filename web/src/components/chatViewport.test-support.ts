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
