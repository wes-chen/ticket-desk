---
description: Resume the autonomous engine loop (runs in ticket-desk-ops)
---

Continue the autonomous loop. The engine lives in your local checkout of
the private ticket-desk-ops repo — cd there first. Then orient: run
`ticket-engine status --live` and read the latest log/ entry. Then work
the ticket queue until it's empty or I stop you.

Rules: respect claims, locks, and the circuit breaker. Implementation and
fixer stay on Codex, reviewer on Claude Opus — never re-point providers
without asking me. Every change needs independent review plus the GTM
gate; never merge your own work. If Codex is out of quota, say so and
only do what's possible without those roles.
