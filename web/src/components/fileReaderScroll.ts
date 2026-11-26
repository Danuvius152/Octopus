export type ReportFilePosition = (note: string | null) => boolean;

function visibleTop(viewport: HTMLElement, top = viewport.getBoundingClientRect().top) {
  const sections = viewport.querySelector(".file-source-sections");
  return top + (sections?.getBoundingClientRect().height ?? 0);
}

export function scrollSourceViewportTo(anchor: HTMLElement | null) {
  const viewport = anchor?.closest<HTMLElement>(".file-viewer-body");
  if (!anchor || !viewport) return;
  // Scroll the reading viewport directly. The source also scrolls horizontally.
  viewport.scrollTop += anchor.getBoundingClientRect().top - visibleTop(viewport);
}

// Source rows and rendered blocks remain mounted across a typography change.
function firstVisibleChild(parent: Element, top: number) {
  let start = 0;
  let end = parent.children.length;
  while (start < end) {
    const middle = Math.floor((start + end) / 2);
    if (parent.children[middle].getBoundingClientRect().bottom > top) end = middle;
    else start = middle + 1;
  }
  return parent.children.item(start);
}

function readingTextAnchor(block: Element, top: number, bottom: number): Range | undefined {
  const document = block.ownerDocument;
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    if (node.length === 0) continue;
    range.selectNodeContents(node);
    if (range.getBoundingClientRect().bottom <= top) continue;
    // The bottom of a text prefix advances with wrapping. Find the first
    // character that reaches the visible lines, including text inside a list.
    let start = 0;
    let end = node.length - 1;
    while (start < end) {
      const middle = Math.floor((start + end) / 2);
      range.setEnd(node, middle + 1);
      if (range.getBoundingClientRect().bottom > top) end = middle;
      else start = middle + 1;
    }
    range.setStart(node, start);
    range.setEnd(node, start + 1);
    const bounds = range.getBoundingClientRect();
    if (bounds.bottom > top && bounds.top < bottom) return range;
  }
}

export function applyReaderSize(scroller: HTMLDivElement, size: string) {
  if (scroller.dataset.readerSize === size) return;
  if (scroller.scrollTop === 0) {
    scroller.dataset.readerSize = size;
    return;
  }
  const reading = scroller.querySelector(".md-github");
  const rows = scroller.querySelector("[data-file-source-rows]");
  const viewport = scroller.getBoundingClientRect();
  const top = visibleTop(scroller, viewport.top);
  let anchor = rows ? firstVisibleChild(rows, top) : reading && firstVisibleChild(reading, top);
  while (anchor?.matches("ol, ul, li, blockquote, section")) {
    const child = firstVisibleChild(anchor, top);
    // A tight Markdown list item can begin with text before its first block.
    if (!child || child.getBoundingClientRect().top > top) break;
    anchor = child;
  }
  const blockBounds = anchor?.getBoundingClientRect();
  const textAnchor = reading && anchor && blockBounds && blockBounds.top < top &&
    !anchor.matches(".md-codeblock, .file-markdown-table")
    ? readingTextAnchor(anchor, top, viewport.bottom) : undefined;
  const before = textAnchor?.getBoundingClientRect() ?? blockBounds;
  scroller.dataset.readerSize = size;
  if (!before || !anchor) return;
  const after = (textAnchor ?? anchor).getBoundingClientRect();
  // An oversized source row or media block can be partially visible.
  const progress = !textAnchor && before.height > 0 ? Math.max(0, top - before.top) / before.height : 0;
  scroller.scrollTop += after.top - before.top + progress * (after.height - before.height);
}
