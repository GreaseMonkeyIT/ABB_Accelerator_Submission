# S1 - PVC I/O cascade (the hero scenario)

Trigger: `./trigger.sh` (touch FLUSH) OR `POST cooling-monitor:8080/flush` (the L4 button) -> sustained
fio (4 jobs x 512m, time_based **60s**, **fsync=2**, O_DIRECT) on the shared PVC. Intensity is
Helm-tunable (FIO_JOBS/SIZE/RUNTIME/FSYNC, no rebuild). fsync=2 means the writer stalls too (LOG-051);
storm duration is no longer load-bearing now the engine event-centres on the detected onset (LOG-054).

| t | expected |
|---|---|
| +0s | cooling-monitor IO storms; node disk io_time climbs |
| +10-20s | dcim_write_seconds p95 jumps (same PVC) |
| +20-45s | timescaledb WAL fsync slows; psi_io stalls; possible probe latency |
| +30-60s | ingest_queue_depth climbs; INSERT rate dips |

Witnesses: io PSI co-pressure (storage-domain pods), `kubelet_volume_stats`, cooling-monitor's
`io_write` deviation (the source signal). No network edge needed. (The CCR actuation hop is NOT an
engine edge -- CCR actuates over MQTT, so it is not eBPF-instrumentable, LOG-078; CCR's own histogram
is the D-004 ground-truth channel, deliberately kept out of the engine.)

Expected verdict (box-measured, LOG-070): **root = cooling-monitor (score 1.00)**, edge
`cooling-monitor -> timescaledb` evidence `[write, pvc, temporal]` (signal psi_io), blast radius =
timescaledb + dcim-bridge; threshold-free. gemma4 narrates it; `case_register = recurrence` on repeats.

Reset: `./reset.sh`; the verdict self-clears ~2-3 min after the storm ends (recency gate, RESET_WINDOW).
Rehearse and log pass/fail in `ledger.csv`. S0 must stay silent before/after (the cool-mon idle journal
is now a bare-minimum unsynced heartbeat, so steady state writes ~nothing to the shared disk).
