import { useCallback } from "react";

/** Keep the browser's Range offsets in the part of the text that stays the same. */
export function useStreamingTextNode(text: string) {
  return useCallback((element: HTMLElement | null) => {
    if (!element) return;
    const node = element.firstChild;
    if (!(node instanceof Text)) {
      element.textContent = text;
      return;
    }
    const previous = node.data;
    if (previous === text) return;
    let prefix = previous.length;
    if (!text.startsWith(previous)) {
      prefix = 0;
      while (prefix < previous.length && previous[prefix] === text[prefix]) prefix++;
    }
    // Replacing a capped preview's display marker keeps the prefix and its
    // selection in the same node.
    node.replaceData(prefix, previous.length - prefix, text.slice(prefix));
  }, [text]);
}
