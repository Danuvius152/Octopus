import { AnnotationMode, getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist/legacy/build/pdf.mjs";
// Vite minifies the patched worker source for the production bundle.
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.mjs?worker&url";
import { version } from "pdfjs-dist/package.json";
export { default as viewerStyles } from "pdfjs-dist/web/pdf_viewer.css?inline";

const resources = `/assets/pdfjs-${version}/`;
const maxPagePixels = 8_000_000;

export function openPdf(bytes: ArrayBuffer, ownerDocument: HTMLDocument) {
  GlobalWorkerOptions.workerSrc = workerUrl;
  return getDocument({
    data: bytes.slice(0),
    ownerDocument,
    disableWorkerRendering: true,
    cMapUrl: `${resources}cmaps/`,
    standardFontDataUrl: `${resources}standard_fonts/`,
    iccUrl: `${resources}iccs/`,
    wasmUrl: `${resources}wasm/`,
    stopAtErrors: true,
    maxImageSize: 32_000_000,
    canvasMaxAreaInBytes: 32_000_000,
  });
}

export function paintPdfPage(pdf: PDFDocumentProxy, pageNumber: number, zoom: number,
  canvas: HTMLCanvasElement, text: HTMLDivElement) {
  let cancelled = false;
  let drawing: RenderTask | undefined;
  let textLayer: TextLayer | undefined;
  const cancel = () => {
    cancelled = true;
    drawing?.cancel();
    textLayer?.cancel();
  };
  const promise = (async () => {
    const page = await pdf.getPage(pageNumber);
    if (cancelled) return;
    const original = page.getViewport({ scale: 1 });
    const scale = zoom * Math.min(4 / 3, 980 / original.width, 1_500 / original.height);
    const viewport = page.getViewport({ scale });
    const pixelRatio = Math.min(window.devicePixelRatio || 1, Math.sqrt(maxPagePixels / (viewport.width * viewport.height)));
    canvas.width = Math.max(1, Math.floor(viewport.width * pixelRatio));
    canvas.height = Math.max(1, Math.floor(viewport.height * pixelRatio));
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    text.style.setProperty("--total-scale-factor", String(scale * page.userUnit));
    textLayer = new TextLayer({ textContentSource: page.streamTextContent(), container: text, viewport });
    drawing = page.render({
      canvas,
      viewport,
      transform: [pixelRatio, 0, 0, pixelRatio, 0, 0],
      annotationMode: AnnotationMode.ENABLE,
    });
    await Promise.all([drawing.promise, textLayer.render()]);
  })().catch((error: unknown) => {
    if (!cancelled) cancel();
    throw error;
  });
  return { promise, cancel };
}
