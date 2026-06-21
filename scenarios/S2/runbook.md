# S2 - large-file I/O starvation (distinct root from S1)

Trigger: `./trigger.sh` -> creates Job `log-archiver-s2` from the suspended `log-archiver` CronJob.
The archiver runs a sustained, CONCURRENT, fsync-heavy `fio` storm (O_DIRECT) on the shared PVC --
the same recipe S1's cooling-monitor uses, but rooted at a DISTINCT workload. A single sequential
`dd` (the old mechanism) only saturated the archiver's own bandwidth and never stalled the DB
(LOG-091); concurrent fsync'd writers thrash the shared spindle so `timescaledb`'s WAL fsync stalls.
The job name is fixed (no timestamp) so the engine resolves the pod to workload `log-archiver` (a
STORAGE member), which is why it couples to the disk and is blamed as the source. Storm is
env-tunable, no rebuild: `S2_SEED_MB` (total on-disk footprint, split across `S2_JOBS`; keep < the
5Gi PVC), `S2_RUNTIME` (duration, default 120s), `S2_JOBS` (default 4), `S2_FSYNC` (default 2).

| t | expected |
|---|---|
| +0s | `log-archiver-s2` pod starts; concurrent `fio` O_DIRECT write storm begins; node disk io_time climbs |
| +20-60s | the archiver pod accrues its 12-sample psi_io window (it enters the engine's coupling set) |
| +30-90s | `timescaledb` psi_io stalls (shared-spindle fsync contention); `dcim_write_seconds` p95 jumps |
| +45-120s | `ingest_queue_depth` climbs / INSERT rate dips; verdict surfaces |

Witnesses: io PSI co-pressure on the storage-domain pods, `kubelet_volume_stats`, the archiver's
`io_write` deviation (the source signal). No network edge needed.

Expected verdict: **root = log-archiver** (DISTINCT from S1's cooling-monitor), edge
`log-archiver -> timescaledb` with evidence `[write, pvc, temporal]`, victim = timescaledb; threshold-free.

Reset: `./reset.sh` (deletes the job; verdict self-clears in ~2-3 min via the recency gate).

Box-verify checklist (post-LOG-091): read `/graph` DURING the storm (pod still `Running`, ~t+60-90s),
not after -- once the Job Completes the pod is gone and its edge render-skips (LOG-090 gap #1).
Confirm (1) `timescaledb` psi_io actually stalls in Grafana (the fio recipe's whole point -- if it
stays flat the storm isn't contending; raise `S2_FSYNC` pressure / `S2_JOBS`), and (2) root =
`log-archiver` with edge `log-archiver -> timescaledb`. If the storm finishes before the window fills,
raise `S2_RUNTIME` (NOT `S2_SEED_MB` -- duration is decoupled from footprint now). A pure
read-starvation root would still need an `io_read` source signal (deferred), but S2's fio storm writes
heavily, so `io_write` attribution should hold.
