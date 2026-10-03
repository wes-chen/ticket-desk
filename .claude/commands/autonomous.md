---
description: Resume the autonomous engine loop (runs in ticket-desk-ops)
---

Continue the autonomous loop. The engine lives in your local checkout of
the private ticket-desk-ops repo — cd there first. Then orient: run
`ticket-engine status --live` and read the latest log/ entry. Then work
the ticket queue until it's empty or I stop you.

Rules: respect claims, locks, and the circuit breaker. No model or provider is
permanently coupled to a role — any agent can take any role. Every agent runs
in its own isolated session with no context contamination between roles. Every
change needs independent review plus the GTM gate; never merge your own work. If a
provider is unavailable, say so and only do what's possible without it.

Operating mode (since 2026-10-02): in-session execution. The engine stays
PAUSED (`ticket-engine start` would need launch quota we don't have) - do
not start it. This session drives: it picks work from the queue and checks
claims, locks and the breaker, then launches each role as its own fresh
session in its own worktree. Default split, chosen by quota and not a
coupling: implementation and fixer run on Codex (headless `codex exec`);
reviewer and GTM run as Claude subagents. A provider that is out of quota
may cover any role, as long as the author and reviewer of a change stay in
distinct sessions.
