import { useSessionStore } from "../stores/sessionStore";

const labels: Record<string, string> = { interrupt: "Stop", answer_question: "Answer", approve_tool: "Allow tool", deny_tool: "Deny tool" };

export function CommandNotices({ sessionId }: { sessionId: string }) {
  const commands = useSessionStore((s) => s.controlCommands);
  const dismiss = useSessionStore((s) => s.dismissControlCommand);
  return <div className="px-4 text-xs space-y-1" aria-label="Command confirmations">
    {Object.values(commands).filter((c) => c.sessionId === sessionId).map((command) => <div key={command.id}
      role={command.state === "failed" || command.state === "unknown" ? "alert" : "status"}>
      {labels[command.operation] ?? command.operation}: {command.state === "pending" ? "awaiting confirmation…" :
        command.state === "accepted" ? "confirmed" : command.message || command.state}
      {command.state !== "pending" && <button type="button" className="ml-2 underline"
        onClick={() => dismiss(command.id)}>Dismiss</button>}
    </div>)}
  </div>;
}
