import { useEffect, useState } from "react";
import { IconPin, IconPinnedOff, IconPlus } from "@tabler/icons-react";
import { applications as applicationList, orderedApplicationPins } from "../lib/applicationList";
import { sidebarApplications } from "../lib/sidebarPins";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { SidebarSectionHeader } from "./SidebarSectionHeader";
import { SortablePin, SortablePins, type DragHandle } from "./SortablePins";
import { AppIcon } from "./AppIcon";

/** The APPLICATIONS section — agent-built web apps, one row each.
 *
 * Rows are quieter than agent rows (no fold, no badge): an icon tile and a
 * name. Selecting one takes over the main pane with the app itself, so the
 * selected state matches the session rows above — tinted pill, accent border.
 * The list is seeded once here and kept live by the `application_*` WS events.
 *
 * Only *pinned* applications are listed, in the user's order (sidebar-pins.md);
 * the Applications page's All tab (the "+", then All) has the rest. An
 * unpinned app joins the list, dimmed, while it's open, building, or has
 * failed a build nobody has looked at yet. A sidebar row is a shortcut, so
 * what you remove from here is the shortcut; archiving is on that page.
 */
export function SidebarApplications() {
  const token = useSessionStore((s) => s.token);
  const accountGeneration = useSessionStore((s) => s.accountGeneration);
  const applications = useSessionStore((s) => s.applications);
  const read = useSessionStore((s) => s.applicationReads.live);
  const ordering = useSessionStore((s) => s.applicationOrdering);
  const pinOrder = useSessionStore((s) => s.applicationPinOrder);
  const [error, setError] = useState<string | null>(null);
  const activeApplicationId = useSessionStore((s) => s.activeApplicationId);
  const mainView = useSessionStore((s) => s.mainView);
  const openApplication = useSessionStore((s) => s.openApplication);
  const openApplicationCreate = useSessionStore((s) => s.openApplicationCreate);
  const unseenFailedApplications = useSessionStore(
    (s) => s.unseenFailedApplications
  );

  useEffect(() => {
    if (token) void applicationList.refresh();
  }, [token, accountGeneration]);

  const reorder = async (orderedIds: string[]) => {
    const current = applicationList.currentGuard();
    setError(null);
    try {
      await applicationList.reorder(orderedIds);
    } catch (e) {
      if (current()) setError(e instanceof Error ? e.message : "Could not save sidebar order");
    }
  };

  const setPinned = async (app: Application, pinned: boolean) => {
    const current = applicationList.currentGuard();
    setError(null);
    try {
      await applicationList.setPinned(app.id, pinned);
    } catch (e) {
      if (current()) setError(e instanceof Error ? e.message : "Could not update sidebar");
    }
  };

  const { present } = sidebarApplications(applications, {
    mainView,
    activeApplicationId,
    unseenFailedApplications,
  });
  const pinned = orderedApplicationPins(applications, pinOrder);

  const renderApp = (app: Application, handle: DragHandle) => {
    const isActive =
      mainView === "application" && app.id === activeApplicationId;
    const isPinned = app.pinned !== false;
    return (
      <div
        {...handle}
        className={`application-item group/app flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-[7px] transition-colors ${
          isActive
            ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
            : "border border-transparent hover:bg-gray-100"
        }${isPinned ? "" : " unpinned"}`}
        onClick={() => openApplication(app.id)}
        title={app.description ? `${app.name} — ${app.description}` : app.name}
      >
        <AppIcon app={app} className="application-icon shrink-0" />
        <span
          className={`application-name truncate text-[13.5px] ${
            isActive ? "font-semibold text-gray-950" : "font-medium text-gray-800"
          }`}
        >
          {app.name}
        </span>
        {app.status !== "ready" && (
          <span
            className={`app-status-dot app-status-${app.status} ml-auto inline-block size-[7px] shrink-0 rounded-full ${
              app.status === "building" ? "bg-primary animate-pulse" : "bg-warn"
            }`}
            aria-label={app.status}
          />
        )}
        <button
          disabled={ordering}
          className={`${
            isPinned ? "btn-application-unpin" : "btn-application-pin"
          } inline-flex size-5 shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 transition-opacity hover:bg-gray-200 hover:text-gray-900 group-hover/app:opacity-100 ${
            app.status === "ready" ? "ml-auto" : ""
          }`}
          onClick={(e) => {
            e.stopPropagation();
            setPinned(app, !isPinned);
          }}
          title={isPinned ? "Unpin from sidebar" : "Pin to sidebar"}
          aria-label={
            isPinned
              ? `Unpin ${app.name} from the sidebar`
              : `Pin ${app.name} to the sidebar`
          }
        >
          {isPinned ? <IconPinnedOff size={13} /> : <IconPin size={13} />}
        </button>
      </div>
    );
  };

  return (
    <div className="application-section shrink-0">
      <SidebarSectionHeader
        label="Applications"
        className="application-header"
        action={{
          icon: <IconPlus size={14} />,
          onClick: openApplicationCreate,
          title: "New application",
          label: "New application",
          className: "btn-application-add",
        }}
      />

      <div className="application-items flex flex-col gap-0.5">
        <SortablePins
          ids={pinned.map((a) => a.id)}
          nameOf={(id) => applications.find((a) => a.id === id)?.name ?? id}
          onReorder={reorder}
          disabled={ordering}
        >
          {pinned.map((app) => (
            <SortablePin key={app.id} id={app.id} disabled={ordering}>
              {(handle) => renderApp(app, handle)}
            </SortablePin>
          ))}
        </SortablePins>
        {present.map((app) => (
          <div key={app.id}>{renderApp(app, {})}</div>
        ))}
        {(error || read.error) && <p role="alert" className="px-2.5 text-xs text-destructive">{error || read.error}</p>}
        {read.loading && !read.loaded && applications.length === 0 && <p className="px-2.5 text-xs">Loading applications…</p>}
        {read.loaded && !read.error && applications.length === 0 && (
          <button
            type="button"
            className="application-empty px-2.5 py-1.5 text-left text-[12.5px] text-gray-700 transition-colors hover:text-gray-900"
            onClick={openApplicationCreate}
          >
            No applications yet — build one.
          </button>
        )}
      </div>
    </div>
  );
}
