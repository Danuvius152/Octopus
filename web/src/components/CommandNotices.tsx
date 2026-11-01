import { useSessionStore } from "../stores/sessionStore";

const labels: Record<string, string> = { interrupt: "Stop", answer_question: "Answer" };

export function CommandNotices({ sessionId }: { sessionId: string }) {
  const commands = useSessionStore((s) => s.controlCommands);
  const dismiss = useSessionStore((s) => s.dismissControlCommand);
  const sessionCommands = Object.values(commands).filter((c) => c.sessionId === sessionId);
  if (sessionCommands.length === 0) return null;
  return <div className="px-4 text-xs space-y-1" aria-label="Command confirmations">
    {sessionCommands.map((command) => <div key={command.id}
      role={command.state === "failed" || command.state === "unknown" ? "alert" : "status"}>
      {labels[command.operation] ?? command.operation}: {command.state === "pending" ? "awaiting confirmation…" :
        command.message || command.state}
      {command.state !== "pending" && <button type="button" className="ml-2 underline"
        onClick={() => dismiss(command.id)}>Dismiss</button>}
    </div>)}
  </div>;
}
