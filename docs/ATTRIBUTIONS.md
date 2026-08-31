# Open-Source Attributions

NexusMind incorporates selected agent patterns inspired by [OpenClaw](https://github.com/openclaw/openclaw), [Hermes Agent](https://github.com/NousResearch/hermes-agent), and [opencode](https://github.com/sst/opencode). These patterns were **reimplemented in Python/FastAPI/Gemini** and integrated into NexusMind's own architecture. No opencode TypeScript/Effect runtime was copied; `opencode-dev` was vendored at `opencode-dev/opencode-dev` for study only.

## Summary

| Pattern | Inspired by | Adaptation in NexusMind |
|---------|-------------|--------------------------|
| Multi-step task decomposition | OpenClaw | Gemini-powered planner with JSON step extraction (`agent/core/planner.py`) |
| Tool registry + sandboxed execution | OpenClaw | `@register_tool` decorator + subprocess isolation (`agent/core/executor.py`) |
| Persistent cross-session memory | Hermes Agent | SQLite + FTS5, hybrid BM25/Jaccard/HRR retrieval, trust scoring (`agent/core/memory/`) |
| Self-evolving skill library | Hermes Agent | Auto-synthesized `SKILL.md` packages, usage telemetry, stale/archive lifecycle, sha256 audit ledger (`agent/core/skill_library.py`) |
| Zero-cost command gate | Hermes + OpenClaw | `/commands` resolved deterministically before the LLM (`agent/core/command_gate.py`) |
| Hallucinated-tool repair ladder | Hermes | Normalize/alias/fuzzy-match against live registry; one corrective re-plan |
| Plan salvage + JSON repair | Hermes + OpenClaw | Truncated plans recovered step-by-step; zero templates — every artifact authored by Gemini |
| Self-improvement reflection | Hermes | Post-task Gemini reflection saved as memory |
| Event-driven scheduling | Hermes | Cloud Pub/Sub (`cloud/pubsub/events.py`) |
| Adaptive step-by-step loop | opencode (`session/prompt.ts` loop) | `agent/core/agent_loop.py` — one decision → one execution → real result feedback → self-correct → verify → done; `40→120` elastic budget, compaction@90 |
| Complexity Router | Custom (NexusMind) | Tier1/2 fast-path vs Tier3 heavy (lazy planner + prompt slicing) |
| Streaming + Global Event Bus | opencode (`session/prompt.ts` streaming, `EventV2Bridge`, `session/status.ts`) | Gemini `generate_content_stream` token deltas + 4 KiB `tool_delta` chunks + `WebSocket /api/ws` + `SSE /api/tasks/live/{id}/stream` + poll + `GET /api/events/live` fan-out |
| `todowrite` + `task` tools | opencode (`tool/todo.ts`, `tool/task.ts`) | `todowrite` full-overwrite (`content/title`, `status`, `priority`) + `task` `explore`/`general` subagents (`agent/core/executor.py`, `agent/models.py`) |
| Human-in-the-loop approval | Custom (NexusMind) | Smart approval gate + Telegram bot (`agent/core/executor.py`, `agent/telegram.py`) |
| Traceability | Custom + opencode `session/status.ts` | In-memory trace collector → live dashboard (WS/SSE/poll) (`agent/observability.py`, `api/dashboard.html`) |
| Multi-key rotation | Custom (NexusMind) | Manual key selection with rate-limit backoff (`agent/core/gemini_client.py`) |

## Detailed Source Mapping (opencode)

Only behavioral ideas were ported — no copy-paste of the Effect/TS runtime. NexusMind's mandatory stack (Gemini, ADK, Firestore, Pub/Sub, Cloud Run per `AGENTS.md`) and Hermes memory/skills remain distinct; opencode contributed the **loop and live-UX shape**, not the runtime.

| opencode source | Behavior | NexusMind location |
|-----------------|----------|--------------------|
| `packages/opencode/src/session/prompt.ts:1081` `runLoop` + `processor.ts` | Decide one tool per turn, stream `LLMEvent.textDelta`, publish `Session/Event/PartDelta`, infinite `agent.steps` until stop, compaction via `session/compaction.ts` | `agent/core/agent_loop.py:530` `run_adaptive_loop` (one decision → one `execute_step` → transcript feedback, `40→120` elastic, `_MAX_CONSECUTIVE_FAILURES=3`, compaction@90, `tool_delta`/`token` emits) |
| `packages/opencode/src/tool/todo.ts` + `session/todo.ts` | First-class `todowrite` overwrites `Todo.Info[]` (`content/status/priority`), persisted + `Todo.Event.Updated` | `agent/core/executor.py:819` `@register_tool("todowrite")`, `agent/models.py:86` `TodoStatus`+`TodoPriority`, `agent_loop.py:867` overwrite + `todo_update` live event |
| `packages/opencode/src/tool/task.ts` + `agent/agent.ts:196` `explore` subagent | `task` delegates to `general`/`explore` (`general` = all tools, `explore` = `grep/glob/read/bash/webfetch` read-only) | `agent/core/executor.py:753` `@register_tool("task")` — `explore` = single `generate_content`, `general` = 8-step mini-loop |
| `packages/opencode/src/session/llm/*` `generate_content_stream` | `llm.stream(request)` per provider turn, streaming deltas | `agent/core/gemini_client.py:660` `generate_content_stream` (`client.models.generate_content_stream`, throttled, quota-aware fallback) → `agent_loop.py:727` forwards `on_event("token")` |
| `tool/shell` + `session/prompt.ts:567` `Stream.decodeText(handle.all)` per-chunk `PartUpdated` | Shell/code stdout streams incrementally to TUI | `agent/core/executor.py:908` `execute_code` / `1020` `run_command` `on_output` 4 KiB drain + `agent_loop.py:834` → `_emit("tool_delta")` |
| `EventV2Bridge` + `session/status.ts:39` + `Session/Todo` events | Global `Status/PartDelta/Todo` bus → TUI/SDK push | `api/main.py:195` `_global_events`+`_ws_clients` + `GET /api/events/live` + `WebSocket /api/ws` (replay 30) + `dashboard.html:612` WS+SSE+poller with dedup |
| `packages/opencode/src/agent/agent.ts:54` `steps` + `permission` | Per-agent `steps` budget + `permission` (allow/ask/deny per tool/pattern) | Kept cost-conscious `40→120` elastic (`agent_loop.py:50`) vs `Infinity`; permission stays as `executor.py:220` smart gate (Telegram vs dashboard) |
| `tool/truncate` + `Truncate.GLOB` whitelisting | Output truncation + `external_directory` whitelisting | `agent_loop.py:64` `_CONTENT_MAX_CHARS=20k` + `_TRANSCRIPT_MAX_CHARS=12k` + collision guard, plus streaming to avoid truncation |

## License Notes

- NexusMind AI is released under the [MIT License](../LICENSE) (see `pyproject.toml:6`).
- OpenClaw, Hermes Agent, and opencode retain their respective original licenses. This project does not redistribute their source code; only reimplemented behavioral patterns are included.

## Required Acknowledgment

> Open-source acknowledgments: NexusMind incorporates selected agent patterns inspired by OpenClaw, Hermes Agent, and opencode. These patterns were reimplemented and integrated into NexusMind's Python/FastAPI/Gemini architecture.
