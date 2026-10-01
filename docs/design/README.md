# Design records

Why ams is the way it is. Read these before changing an invariant.

| File | What it is |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | Every non-trivial decision, D1–D33: what was chosen, why it beat the alternatives, the rejected alternatives and when the choice breaks. Two kinds of entries: **Facts** (verified) and **Open / weak signals** (small-n results, suspicions, the next test). |
| [`PLAN-core.md`](PLAN-core.md) | The plan and interfaces for core mode (1.1.0), the platform mode that hosts the api core. |
| [`PROGRESS.md`](PROGRESS.md) | Done / next, in the order it happened, with measurements. |
| [`history/`](history/) | Working notes from ams 1.0.0 (the manifest mode removed in 2.0.0): plans, live-run reports, diagnoses, pool migration, and `evidence/` (raw logs and the probe scripts that produced them). Historical observations, not current behaviour and not planning constants. |

These were written by the agents that built ams (Claude Code sessions, driven
by a person), as they worked: terse, dated, with the failed attempts left in.
Host names in them (`racknerd`, `phm`, droplet names) are machines of the
author's; IP addresses were replaced with documentation ranges
(`203.0.113.0/24`, `100.64.0.0/10`).
