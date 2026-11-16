import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { Virtuoso, type StateSnapshot, type VirtuosoHandle } from "react-virtuoso";

/** Real-library experiment for the preparation owner's public measurement seam. */
export function mountMeasurementProbe() {
  const container = document.createElement("div");
  container.style.cssText = "position:fixed;inset:0 auto auto 0;width:700px;height:400px;z-index:10000;background:white";
  document.body.append(container);
  const root = createRoot(container);
  let handle: VirtuosoHandle | null = null;
  let tailHeight = 80;
  const measurements: { height: number; installed: StateSnapshot | null }[] = [];
  const snapshot = (): StateSnapshot | null => {
    let state: StateSnapshot | null = null;
    handle?.getState((value) => { state = value; });
    return state;
  };
  const render = () => root.render(createElement(Virtuoso, {
    ref: (value: VirtuosoHandle | null) => { handle = value; },
    style: { height: 400 }, totalCount: 40, defaultItemHeight: 80,
    itemContent: (index: number) => createElement("div", { style: { height: index === 39 ? tailHeight : 80 } }, `Probe row ${index}`),
    itemSize: (element: HTMLElement) => {
      const height = element.getBoundingClientRect().height;
      if (element.dataset.index === "39") measurements.push({ height, installed: snapshot() });
      return height;
    },
  }));
  render();
  return {
    tail: () => handle?.scrollToIndex({ index: 39, align: "end" }),
    grow: () => { tailHeight = 1000; render(); },
    snapshot,
    measurements: () => [...measurements],
    geometry: () => {
      const viewport = container.querySelector<HTMLElement>("[data-virtuoso-scroller]");
      return { height: viewport?.clientHeight, top: viewport?.scrollTop, extent: viewport?.scrollHeight,
        rows: Array.from(container.querySelectorAll("[data-index]")).map((row) => row.getAttribute("data-index")) };
    },
    dispose: () => { root.unmount(); container.remove(); },
  };
}
