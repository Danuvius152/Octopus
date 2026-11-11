# 消息时间、输入历史与模型选择

> **Status:** implemented — 原始时间戳、仅人工输入历史和会话模型控制。

## Message timestamps

Messages retain their own `created_at` for durable history and live display.
Imported native timestamps preserve the original event time; a consolidated text
block keeps its first part's time, and a tool call keeps its own time rather than
the tool result's. Missing historical times stay absent instead of being invented.

`MessageTime` belongs inside its message row. Its geometry must not cover text,
the rewind control or a bordered tool card's stroke, and must not reflow the
transcript on hover. Touch displays show dimmed timestamps without needing hover.
The useful lesson from the original fixes is preserved as a verification rule:
assert box placement and intersections, not only that a label exists.

## Composer history

ArrowUp walks backward through human-entered prompts, ArrowDown forward, and
moving past the newest restores the draft present before navigation. At the
oldest entry the key remains consumed. History activates only on the first/last
line as appropriate; an open slash menu owns its arrow keys. Only nonblank
user text is eligible, and consecutive duplicate prompts collapse. Structured
`tool_input.request_context.source` takes precedence: `human` remains eligible
even when its text resembles an injection marker, while non-human sources are
excluded. Only history without that provenance falls back to the shared legacy
prefix predicate (BG results, agent replies/questions/errors and scheduled fires).
Rewind uses the same provenance-first distinction; a markerless legacy machine
message cannot reliably be classified from text alone.

## Model selection

`/model` and the header open the same settings dialog; `/model <name>` applies a
session override for later turns. Independent inheritance, native model metadata,
effort and speed are maintained in [model settings](model-reasoning.md).
The original plan's missing model endpoint/header are already implemented.
Persistent effective-model/source display and additional input controls remain
in [the input-control plan](../plans/session-input-controls.md); future display
must distinguish the currently captured request from next-turn inherited settings.

## Verification

Code: `web/src/lib/composerHistory.ts`, `web/src/lib/injectedTurns.ts` and
`web/src/components/ChatView.tsx`. Regression entry points include
`web/src/lib/composerHistory.test.ts` and the message-time browser scenarios.
Preserve user draft/caret behavior when changing either prompt history or slash
menus; timestamps must retain original identity across live updates and reloads.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/composer-and-time.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.
