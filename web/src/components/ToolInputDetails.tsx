import { presentToolInput, type ToolInputSource } from "../lib/messagePresentation";

/** Present tool arguments without mixing in the runtime's saved audit envelopes. */
export function ToolInputDetails({ message }: { message: ToolInputSource }) {
  if (!message.tool_input) return null;
  const { text, raw } = presentToolInput(message);

  return <>
    <pre className="tool-detail max-h-80 overflow-auto whitespace-pre-wrap break-words border-t border-gray-300 bg-card px-4 py-2.5 font-mono text-[11.5px] text-gray-900">
      {text}
    </pre>
    {raw && <details className="px-4 pb-2 text-xs text-muted-foreground">
      <summary className="cursor-pointer">Raw tool data</summary>
      <pre aria-label="Raw tool input" className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-[11.5px]">
        {JSON.stringify(raw, null, 2)}
      </pre>
    </details>}
  </>;
}
