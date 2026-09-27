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
