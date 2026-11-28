import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from "pdfjs-dist";
import frameStyles from "./PdfReaderFrame.css?inline";

type Renderer = typeof import("./pdfRenderer");
type LoadedPdf = { pdf: PDFDocumentProxy; renderer: Renderer };
const zoomLevels = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5];

export function PdfReaderFrame({ bytes, ownerDocument, interactive, onReady, onError, onReaderAction }: {
  bytes: ArrayBuffer;
  ownerDocument: HTMLDocument;
  interactive: boolean;
  onReady: () => void;
  onError: () => void;
  onReaderAction: () => boolean;
}) {
  const [loaded, setLoaded] = useState<LoadedPdf>();
  const [page, setPage] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(3);
  const firstPaint = useRef(false);
  const zoom = zoomLevels[zoomIndex];
  const reportPaint = useCallback(() => {
    if (firstPaint.current) return;
    firstPaint.current = true;
    onReady();
  }, [onReady]);
  const reportFailure = useCallback(() => {
    if (!firstPaint.current) onError();
  }, [onError]);

  useEffect(() => {
    let retired = false;
    let loading: PDFDocumentLoadingTask | undefined;
    void (async () => {
      try {
        const renderer = await import("./pdfRenderer");
        if (retired) return;
        loading = renderer.openPdf(bytes, ownerDocument);
        const pdf = await loading.promise;
        if (!retired) setLoaded({ pdf, renderer });
      } catch {
        if (!retired) onError();
      }
    })();
    return () => {
      retired = true;
      void loading?.destroy().catch(() => {});
    };
  }, [bytes, ownerDocument, onError]);

  const turnPage = (pageNumber: number) => {
    if (onReaderAction()) setPage(pageNumber);
  };
  const changeZoom = (index: number) => {
    if (onReaderAction()) setZoomIndex(index);
  };
  const handleTab = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
    const stops = event.currentTarget.querySelectorAll("button:not(:disabled), [tabindex='0']");
    const edge = stops[event.shiftKey ? 0 : stops.length - 1];
    // The portal bubbles to the dialog, whose focus scope sees one iframe.
    // Let the browser traverse its contents and the dialog handle the edges.
    if (event.target !== edge) event.stopPropagation();
  };

  return <div className="pdf-reader" inert={!interactive} aria-hidden={!interactive || undefined} onKeyDown={handleTab}>
    <style>{frameStyles}</style>
    {loaded && <>
      <style>{loaded.renderer.viewerStyles}</style>
      <div className="pdf-controls" role="group" aria-label="PDF reader controls">
        <button type="button" disabled={page <= 1} onClick={() => turnPage(page - 1)}>Previous page</button>
        <span aria-live="polite">Page {page} of {loaded.pdf.numPages}</span>
        <button type="button" disabled={page >= loaded.pdf.numPages} onClick={() => turnPage(page + 1)}>Next page</button>
        <button type="button" disabled={zoomIndex === 0} onClick={() => changeZoom(zoomIndex - 1)}>Zoom out</button>
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <button type="button" disabled={zoomIndex === zoomLevels.length - 1} onClick={() => changeZoom(zoomIndex + 1)}>Zoom in</button>
      </div>
      <div className="pdf-sheet-container" key={page} tabIndex={0} role="region" aria-label={`PDF page ${page}`}>
        <PdfPage key={zoom} document={loaded} page={page} zoom={zoom} onPaint={reportPaint} onError={reportFailure} />
      </div>
    </>}
  </div>;
}

function PdfPage({ document, page, zoom, onPaint, onError }: {
  document: LoadedPdf;
  page: number;
  zoom: number;
  onPaint: () => void;
  onError: () => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const text = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "painted" | "failed">("loading");

  useEffect(() => {
    let retired = false;
    const overlay = text.current!;
    const drawing = document.renderer.paintPdfPage(document.pdf, page, zoom, canvas.current!, overlay);
    void drawing.promise.then(() => {
      if (!retired) setStatus("painted");
    }, () => {
      if (retired) return;
      setStatus("failed");
      onError();
    });
    return () => {
      retired = true;
      drawing.cancel();
      overlay.replaceChildren();
    };
  }, [document, page, zoom, onError]);
  useEffect(() => {
    if (status === "painted") onPaint();
  }, [status, onPaint]);

  return <>
    {status === "loading" && <p role="status">Drawing page…</p>}
    {status === "failed" && <p role="alert">Couldn't draw this page. Try another page or open the original file.</p>}
    <div className="pdf-sheet" style={{ visibility: status === "painted" ? "visible" : "hidden" }}>
      <canvas ref={canvas} aria-hidden="true" />
      <div className="textLayer" ref={text} />
    </div>
  </>;
}
