---
description: Resume the autonomous engine loop (runs in ticket-desk-ops)
---

Continue the autonomous loop. The engine lives in your local checkout of
the private ticket-desk-ops repo — cd there first. Before queue selection,
claims, or dispatch, read the local checkpoint:

```bash
python3 harness/interactive_tick.py status
python3 harness/interactive_tick.py resume
```

Use the reviewed CLI from private main. If it is unavailable, state is invalid,
or either command holds, reconcile that condition before taking new work.
Read `harness/README.md`'s interactive checkpoint contract and the CLI `--help`.
Checkpoint files and operation specs stay local and untracked, never in this
public repo. The default directory is
`~/.local/state/ticket-desk-actors/interactive/` (`XDG_STATE_HOME` is honoured).

If `resume` returns an open tick, continue its `next_step` and `next_actions`.
Re-read its PR heads, claims and review evidence on GitHub, and check the live
agent registry before acting on recorded agents; worktree existence is not
liveness. Reconcile a pending operation before retrying it. Never start a
replacement tick to evade a hold. For a legacy checkpoint, read and reconcile
its live work first, then use `archive-legacy --name ... --reason ...` to retain
the original evidence. Do not delete or hand-edit state to clear a hold.

After checkpoint recovery, run `ticket-engine status --live` and read the latest
`log/` entry. If there is no open tick, use `start --next-step ...` with one to
three `--next-action ...` arguments before the first claim or dispatch. Then
work the ticket queue until it is empty or I stop you.

During the tick, prefix the commands below with
`python3 harness/interactive_tick.py`:

- Before each supported GitHub mutation, use `apply --op-id ... --kind ...
  --spec <local-json-file>`. Use a stable operation id and the exact spec schema
  in the CLI; `apply` durably records pending, reconciles, writes, then records
  done. Claims, releases and review verdicts use `comment` with the corresponding
  category. This journal does not replace claim policy, review or merge gates.
  A GTM merge's operation marker must be in the exact squash text checked by
  the gate. Do not make a separate raw GitHub write and record it afterwards.
- Before every subagent wave, use `plan` to persist the next one to three actions,
  current PR numbers and head SHAs (`--in-flight-prs <json-array-file>`), and
  pending reviews (`--pending-reviews <json-array-file>`). Run `flush-wave
  --wave <unique-wave-id>` unconditionally, before launching any agent.
- For each dispatch, call `apply --kind dispatch` with its role, target,
  agent_name and branch spec before launching it. Its initial exit 2 saying
  "dispatch ... prepared" is expected: verify the matching durable pending
  dispatch, then launch exactly once and record the real id with `dispatch-done
  --op-id ... --agent-id ...`. A pending dispatch on resume requires live
  registry reconciliation, never an automatic relaunch. Use
  `dispatch-not-launched` only with evidence that no launch happened.
- Record completed state changes and agent outcomes immediately with
  `event --day-log ...`, and refresh the plan. For attempted writes with uncertain
  outcomes, keep the original operation id and reconcile; a missing listing
  alone does not justify `confirm-absent`, a new id, or a duplicate write.
  `abandon` requires a reason and a fresh read; it does not prove absence.
  If a required mutation is unsupported, report the gap and preserve the
  pending work rather than bypassing write-ahead recording.
- At a normal stop, persist any remaining handoff and run `close`. If close
  holds, leave the checkpoint open and report why. Wave flushes and close write
  local day-log and review outboxes; they do not publish the ledgers. Preserve
  those outboxes and report their publication handoff under the private repo's
  current ledger policy. Do not describe a local flush as a published log.

Rules: respect claims, locks, and the circuit breaker. No model or provider is
permanently coupled to a role — any agent can take any role. Every agent runs
in its own isolated session with no context contamination between roles. Every
change needs independent review plus the GTM gate; never merge your own work. If a
provider is unavailable, say so and only do what's possible without it.
