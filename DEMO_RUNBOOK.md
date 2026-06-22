# Demo Runbook — SiliconKnights Edge Causal AIOps

Press-this / say-this for a ~10-minute live demo. Everything runs on one K3s box; the
dashboard is the single pane of glass (NodePort `30080`, reachable over Tailscale).
Scenarios fire from the dashboard **Scenario console** (or the `scenarios/<id>/trigger.sh`
scripts / `POST /api/scenarios/<id>/trigger`).

## Pre-flight (once, before the audience)

1. `./deploy/skctl status` — every factory pod `Running`; in `aiops`, engine / api / dashboard `Running`.
2. **Warm the narrator:** open the dashboard once (the 5 s narrative poll loads gemma4), or
   `curl -s localhost:8088/api/narrative >/dev/null`. First load is ~20–30 s; `keep_alive` holds it warm.
3. **Confirm baseline silence (S0):** `/api/graph` reads `findings: []`, no root, verdict **steady**. On a
   *fresh/cold* deploy this takes **~15–20 min** — detection is deviation-based, so the engine must first learn each
   pod's steady-state PSI baseline (and TimescaleDB must finish its initial-population I/O); until then you'll see
   transient TimescaleDB I/O findings that clear on their own. A warm redeploy (engine-memory kept) is silent in
   ~5 min. **Don't fire S1 until S0 is silent**, or the warm-up noise muddies the result.

## S0 — steady state (the control)  ~30 s

- **Show:** the dashboard at rest — no root, no hot edges, verdict "steady".
- **Say:** "Detection is deviation-based against a learned per-pod baseline, so normal factory load
  produces *nothing*. No thresholds firing on healthy pods."

## S1 — PVC I/O cascade (the hero)  ~90 s

- **Press:** Scenario console → **S1 Fire** (or `bash scenarios/S1/trigger.sh`).
- **Wait** ~45–50 s (the engine needs ≥12 samples in the window; trigger on *settled* pods, not right
  after a rollout).
- **Show:** the 3D causal graph lights an edge **cooling-monitor → timescaledb**; root-cause ranking
  #1 = **cooling-monitor**; blast radius = timescaledb + dcim-bridge; the gemma4 narrative names the
  source and victim.
- **Say:** "Same shared disk, three pods. PSI alone only sees the *victims* stalling. We attribute the
  *source* from a per-pod write signal — cooling-monitor is the aggressor — and draw a threshold-free
  causal edge: correlation + a shared-volume witness + temporal order. No `value > limit` anywhere in
  the path."
- **Reset:** Scenario console → S1 reset (or `scenarios/S1/reset.sh`). The verdict self-clears a few minutes
  (~3–5) after the 120s storm ends — the recency gate decays the hot edge back to its structural floor; the longer
  storm (needed for reliable rooting) lengthens the tail. Point this out: "no lingering hot edge, no manual cleanup."

## S5 — memory leak → OOM forecast  ~60 s

- **Press:** Scenario console → **S5 Fire** (patches `vision-qc` `LEAK_ENABLED=true`).
- **Show:** the AI insight feed / verdict raises an **OOM early-warning with an ETA** (working_set →
  memory limit) *before* the kill.
- **Say:** "This is forecasting, not just detection — we project the leak to the cap and warn before
  the OOM, deterministically (no model needed for the number)."
- **Reset:** Scenario console → S5 reset (`LEAK_ENABLED=false`).

## The four judge questions — honest answers

- **Q3 — are services influencing each other?** **Demonstrated live** on disk: S1 shows cooling-monitor
  starving timescaledb over a shared PVC.
- **Q4 — which workloads need optimization?** **Live**: `/api/recommendations` → right-sizing in
  scheduler verbs + a per-namespace fairness (Gini) index.
- **Q1 — which pod causes CPU spikes?** Engine is multi-signal (psi_cpu ready); a true cross-pod CPU
  demo needs node saturation we don't force on the 16-thread reference box — *engine-ready,
  physics-gated*.
- **Q2 — PVC I/O ↔ pod restarts?** The I/O cascade is live (S1); the restart-probe linkage is future
  work.

## S2 — attribution tuning in progress

S2 (large-file write storm via `log-archiver`) exercises the engine on a tougher case: a *short-lived* CronJob writer.
The engine reliably sees the disk stress on timescaledb, but **precise source attribution here is still being tuned** —
the on-demand source has no steady-state PSI baseline, and a persistent backbone edge can still rank ahead of it. So
**lead with S1 for the disk-causality story** (clean, fully attributed) and treat S2 as a forward-looking case.

- **If asked about S2:** "Attributing an on-demand write job is the refinement we're working on — the source needs a
  steady-state baseline to deviate from, and we're tuning the ranking so a persistent edge can't outrank a live
  source. S1 is our proven disk-causality path."

## Fallbacks

- **Narrator slow / cold:** the verdict card always renders from the deterministic template; the gemma4
  prose is additive. The demo never blocks on the model.
- **Onset looks late:** trigger on settled pods — fresh pods need ~60 s of sample warm-up before the
  window fills.
- **Verdict won't clear:** after a 120s storm the hot edge takes ~3–5 min to decay below the hot threshold — wait the recency window, or hit the scenario reset to force it.
