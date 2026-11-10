import { useEffect } from "react";
import { IconActivity, IconBolt, IconClock, IconSettings } from "@tabler/icons-react";
import { useSessionStore, type Schedule } from "../stores/sessionStore";
import { SidebarSectionHeader } from "./SidebarSectionHeader";
import { schedules as scheduleList } from "../lib/scheduleList";
import { modelCredentials } from "../lib/modelCredentials";

/** Format the next fire as the design writes it — "next 09:00". Anything
 * further out than today says which day, because "next 09:00" on a Monday
 * schedule read on Friday is a lie. */
function nextRunLabel(schedules: Schedule[]): string | null {
  const upcoming = schedules
    .filter((s) => s.enabled && s.next_run_at)
    .map((s) => new Date(s.next_run_at as string))
    .filter((d) => !Number.isNaN(d.getTime()))
    .sort((a, b) => a.getTime() - b.getTime());
  const next = upcoming[0];
  if (!next) return null;
  const time = next.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = next.toDateString() === new Date().toDateString();
  return sameDay ? `next ${time}` : `next ${next.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

/** The MANAGE group — the system half of the sidebar.
 *
 * Three summary rows, each a link to a full page in the main area (the
 * console design moved these out of dialogs). The right-hand summary is the
 * point: it carries the count plus a health dot, so a lapsed credential or a
 * broken connector is visible from anywhere in the app without opening
 * anything.
 */
export function SidebarManage() {
  const token = useSessionStore((s) => s.token);
  const accountGeneration = useSessionStore((s) => s.accountGeneration);
  const mainView = useSessionStore((s) => s.mainView);
  const openManage = useSessionStore((s) => s.openManage);
  const schedules = useSessionStore((s) => s.schedules);
  const scheduleRead = useSessionStore((s) => s.scheduleRead);
  const credentials = useSessionStore((s) => s.credentials);
  const credentialRead = useSessionStore((s) => s.credentialRead);
  const installations = useSessionStore((s) => s.connectorInstallations);
  const installationRead = useSessionStore((s) => s.connectorRead);
  const identity = useSessionStore((s) => s.identity);

  useEffect(() => {
    if (token) {
      void scheduleList.refresh();
      void modelCredentials.refresh();
    }
  }, [token, accountGeneration]);

  const needsReconnect = installations.some((i) => i.needs_reconnect);
  const connectorError = needsReconnect || !!installationRead.error;
  const credentialError = !!credentialRead.error || credentials.some(
    (c) => c.needs_reconnect
  );
  const next = nextRunLabel(schedules);

  return (
    <div className="manage-section shrink-0">
      <div className="mx-1.5 mt-3.5 border-t border-gray-300" />
      <SidebarSectionHeader label="Manage" className="manage-header pt-3" />

      <div className="manage-items flex flex-col gap-0.5">
        <ManageRow
          icon={<IconClock size={14} />}
          label="Schedules"
          className="btn-manage-schedules"
          active={mainView === "schedules"}
          onClick={() => openManage("schedules")}
          summary={
            <>
              {scheduleRead.error ? "not synchronized" : scheduleRead.loading && !scheduleRead.loaded && !schedules.length ? "loading…" : schedules.length}
              {next && <span className="text-gray-600"> · {next}</span>}
            </>
          }
        />
        <ManageRow
          icon={<IconBolt size={14} />}
          label="Connectors"
          className="btn-manage-connectors"
          active={mainView === "connectors"}
          onClick={() => openManage("connectors")}
          summary={
            <>
              {installationRead.error ? "not synchronized" : needsReconnect ? (
                <span className="text-warn-foreground">needs reconnect</span>
              ) : (
                installationRead.loading && !installationRead.loaded && !installations.length ? "loading…" : installations.length
              )}
              <span
                className={`inline-block size-1.5 rounded-full ${
                  connectorError ? "bg-warn" : "bg-success"
                }`}
              />
            </>
          }
        />
        <ManageRow
          icon={<IconSettings size={14} />}
          label="Harness"
          className="btn-manage-harness"
          active={mainView === "harness"}
          onClick={() => openManage("harness")}
          summary={
            <>
              {credentialRead.error ? "not synchronized" : credentialError ? (
                <span className="text-warn-foreground">needs reconnect</span>
              ) : (
                credentials.length
              )}
              <span
                className={`inline-block size-1.5 rounded-full ${
                  credentialError ? "bg-warn" : "bg-success"
                }`}
              />
            </>
          }
        />
        {/* Installation-wide operations belong to an identified administrator. */}
        {identity?.user_id && identity.is_admin && (
          <ManageRow
            icon={<IconActivity size={14} />}
            label="Monitor"
            className="btn-manage-monitor"
            active={mainView === "monitor"}
            onClick={() => openManage("monitor")}
            summary={<span className="text-gray-600">30d</span>}
          />
        )}
      </div>
    </div>
  );
}

function ManageRow({
  icon,
  label,
  summary,
  active,
  onClick,
  className,
}: {
  icon: React.ReactNode;
  label: string;
  summary: React.ReactNode;
  active: boolean;
  onClick: () => void;
  className: string;
}) {
  return (
    <button
      type="button"
      className={`manage-item ${className} flex w-full items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-left transition-colors ${
        active
          ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
          : "border border-transparent hover:bg-gray-100"
      }`}
      onClick={onClick}
      /* The label is hidden when the sidebar is collapsed, so the icon needs
         to say what it is some other way. */
      title={label}
    >
      <span className="inline-flex w-5 shrink-0 justify-center text-gray-700">
        {icon}
      </span>
      <span
        className={`manage-label flex-1 truncate text-[13.5px] ${
          active ? "font-semibold text-gray-950" : "text-gray-800"
        }`}
      >
        {label}
      </span>
      <span className="manage-summary flex shrink-0 items-center gap-1.5 font-mono text-[10.5px] text-gray-700">
        {summary}
      </span>
    </button>
  );
}
