import { useEffect } from "react";

/** Sets app height and offset through `--app-h` and `--app-top`.
 *
 * On iOS the layout viewport does not shrink when the
 * software keyboard (or the form-assistant bar an external keyboard brings)
 * covers part of the screen, so an absolutely-positioned root keeps its full
 * height and the composer ends up underneath the keyboard.
 *
 * While an editable element has focus, follow the visual viewport's height
 * and offset to keep the composer above the keyboard. Otherwise use
 * `innerHeight` and offset 0: iOS can retain a stale, smaller visual viewport
 * after the keyboard closes. If visualViewport is unavailable, use that same
 * innerHeight fallback.
 *
 * Syncs are re-run a few times after focus and orientation changes, because
 * the measurement that matters is the one at the END of the keyboard
 * animation and iOS does not reliably send a resize then.
 */
export function useViewportHeight() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;
    let timers: number[] = [];

    const isEditing = () => {
      const el = document.activeElement as HTMLElement | null;
      if (!el) return false;
      return (
        el.tagName === "INPUT" ||
        el.tagName === "TEXTAREA" ||
        el.tagName === "SELECT" ||
        el.isContentEditable
      );
    };

    function sync() {
      const editing = vv != null && isEditing();
      const height = editing ? vv!.height : window.innerHeight;
      const top = editing ? vv!.offsetTop : 0;
      root.style.setProperty("--app-h", `${height}px`);
      root.style.setProperty("--app-top", `${top}px`);
    }

    /** Sync now, then again as the keyboard finishes moving. */
    function syncSettling() {
      sync();
      timers.forEach((t) => clearTimeout(t));
      timers = [120, 320, 650].map((ms) => window.setTimeout(sync, ms));
    }

    sync();
    vv?.addEventListener("resize", sync);
    vv?.addEventListener("scroll", sync);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", syncSettling);
    window.addEventListener("focusin", syncSettling);
    window.addEventListener("focusout", syncSettling);
    window.addEventListener("pageshow", syncSettling);
    // Last resort for the case no event covers: the keyboard dismissed while
    // the field kept focus, leaving a stale height nothing corrects. The next
    // tap anywhere re-measures, so the page heals itself instead of staying
    // short until reload.
    window.addEventListener("touchend", syncSettling, { passive: true });

    return () => {
      timers.forEach((t) => clearTimeout(t));
      vv?.removeEventListener("resize", sync);
      vv?.removeEventListener("scroll", sync);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", syncSettling);
      window.removeEventListener("focusin", syncSettling);
      window.removeEventListener("focusout", syncSettling);
      window.removeEventListener("pageshow", syncSettling);
      window.removeEventListener("touchend", syncSettling);
    };
  }, []);
}
