#!/usr/bin/env python3
"""L3 correlation service (P4).

Polls the L2 aggregator's /window (per-pod signal vectors) and /events (anomaly
seeds), builds the engine inputs, runs one deterministic pass, and serves the
latest CausalGraph at /graph. No language model anywhere in this process; the
single LLM lives at L4.

v0 witness construction (until Caretta/OBI land): shared-storage relations come
from the known storage-domain workloads (one physical disk via local-path), and
PSI co-pressure comes from pods whose signal is elevated in the same window.
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from engine.forecast import incipient_findings
from engine.gate import Witness
from engine.merge import merge_graphs
from engine.pipeline import run_pass
from engine.state import GraphMemory, MemoryConfig

WINDOW_URL = os.environ.get("WINDOW_URL", "http://aggregator.aiops.svc:9000/window")
EVENTS_URL = os.environ.get("EVENTS_URL", "http://aggregator.aiops.svc:9000/events")
SIGNALS    = [s.strip() for s in os.environ.get("ENGINE_SIGNALS", "psi_io,psi_cpu,psi_mem").split(",") if s.strip()]
PRIMARY    = SIGNALS[0]                                          # keeps the original memory-db path
# Resource-class source/aggressor signal per psi class -> writer/hog leads staller (source attribution).
# psi_mem has none: a memory leak is self-caused (source == victim), so it forms no cross-pod edge --
# instead it gets an OOM FORECAST: project working_set to the pod's memory limit (FORECAST_* below).
SIGNAL_SOURCES = {"psi_io": "io_write", "psi_cpu": "cpu"}
FORECAST_SIGNAL = os.environ.get("FORECAST_SIGNAL", "mem")           # working_set bytes: the OOM ramp
FORECAST_LIMIT  = os.environ.get("FORECAST_LIMIT", "mem_limit")      # memory limit (kube-state): the cap
FORECAST_HORIZON_S = float(os.environ.get("FORECAST_HORIZON_S", "900"))  # warn only if OOM is within this window
FORECAST_MIN_FRAC  = float(os.environ.get("FORECAST_MIN_FRAC", "0.6"))   # warn only once working_set is past this fraction of the limit (drops transient/low-level climbs)
INTERVAL   = int(os.environ.get("ENGINE_INTERVAL", "10"))        # seconds between passes
PORT       = int(os.environ.get("ENGINE_PORT", "9100"))
COPR_MIN   = float(os.environ.get("COPRESSURE_MIN", "0.10"))     # signal level that counts as "stalled"
ANALYSIS_WINDOW = int(os.environ.get("ANALYSIS_WINDOW", "36"))   # samples (~3min): the WHOLE pass (detect+correlate+order) looks back over the recent disturbance, not the 15-min ring. Match to event timescale; not a resource limit.
RESET_WINDOW    = int(os.environ.get("RESET_WINDOW", "24"))      # samples (~2min): an onset is a CURRENT incident only if the pod still deviates within this recent tail -> the verdict clears ~RESET_WINDOW after a storm ends, not when it scrolls out of the 15-min ring
GRID_STEP_S = float(os.environ.get("POLL_S", "5"))               # aggregator scrape cadence = the time-alignment grid step (resample all pods onto a shared wall-clock axis)
MEMORY_DB  = os.environ.get("MEMORY_DB", "/var/lib/skn/memory/l3-memory.db")
STORAGE    = [s.strip() for s in os.environ.get(
    "STORAGE_WORKLOADS", "cooling-monitor,dcim-bridge,log-archiver,timescaledb").split(",")]

def _config(signal):
    """One MemoryConfig per signal. Only the disk domain (psi_io) has a STABLE coupling topology
    (the shared-PVC quartet), so only it keeps a structural backbone. CPU/mem contention is
    transient with no fixed topology, so those edges are PURELY LIVE (no prior, no floor) -- they
    appear during real contention and vanish after, instead of accreting a false permanent backbone."""
    structural = signal == "psi_io"
    return MemoryConfig(
        signal=signal,
        alpha=float(os.environ.get("EDGE_ALPHA", "0.4")),
        decay=float(os.environ.get("EDGE_DECAY", "0.1")),
        show=float(os.environ.get("EDGE_SHOW", "0.6")),
        hide=float(os.environ.get("EDGE_HIDE", "0.25")),
        prior=float(os.environ.get("EDGE_PRIOR", "0.2")) if structural else 0.0,
        floor_frac=float(os.environ.get("EDGE_FLOOR_FRAC", "0.4")) if structural else 0.0,
        tau_merge=float(os.environ.get("CASE_TAU_MERGE", "0.85")),
        tau_family=float(os.environ.get("CASE_TAU_FAMILY", "0.60")),
        base_alpha=float(os.environ.get("BASE_ALPHA", "0.05")),
        dev_k=float(os.environ.get("DEV_K", "4.0")),
        mad_floor=float(os.environ.get("MAD_FLOOR", "0.01")),
        base_min_n=int(os.environ.get("BASE_MIN_N", "12")),
    )


def _db_for(signal):
    """Each signal gets its own SQLite memory. The PRIMARY keeps the original path so existing
    psi_io history (edges, cases, baselines) carries over; the others get a `.<signal>` suffix."""
    if signal == PRIMARY:
        return MEMORY_DB
    base, ext = os.path.splitext(MEMORY_DB)
    return f"{base}.{signal}{ext}"


# One independent causal memory per resource class (different witness, different case family).
_memory = {s: GraphMemory(_db_for(s), _config(s)) for s in SIGNALS}
_lock = threading.Lock()
_graph = merge_graphs({s: m.bootstrap_graph() for s, m in _memory.items()}, primary=PRIMARY)


def _fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def workload(pod):
    """cooling-monitor-59584cbf7d-6szhd -> cooling-monitor (drop replicaset + pod hash)."""
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def _epoch(ts):
    """Aggregator stamps each sample with its poll time (Go RFC3339). -> epoch seconds, or None."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def build_inputs(window, events):
    """window: {ns/pod/signal: [{ts,value,...}]}  ->  ({signal: {pod: vec}}, breach), TIME-ALIGNED.

    Collects every psi signal in SIGNALS plus each resource's source signal (io_write, cpu),
    resampled onto ONE shared wall-clock grid by each sample's `ts`. The aggregator ring is a
    positional append, but psi is gappy and pods restart, so column i drifts across pods; the grid
    makes column k the same instant for all pods -- the precondition lagged cross-correlation
    assumes. Stale pods (last sample older than the grid) drop out for free (retires LOG-048).
    """
    step, n = GRID_STEP_S, 180
    # psi victims + their source/aggressor signals + the OOM-forecast ramp (working_set) & its limit
    wanted = set(SIGNALS) | set(SIGNAL_SOURCES.values()) | {FORECAST_SIGNAL, FORECAST_LIMIT}
    raw = {sig: {} for sig in wanted}
    latest = 0.0
    for key, samples in window.items():
        parts = key.split("/")
        if len(parts) < 3 or parts[-1] not in wanted or not samples:
            continue
        pts = sorted((t, s["value"]) for s in samples if (t := _epoch(s.get("ts"))) is not None)
        if len(pts) >= 12:
            raw[parts[-1]][parts[1]] = pts
            latest = max(latest, pts[-1][0])

    grid = [latest - step * (n - 1 - k) for k in range(n)]

    def to_vectors(per_pod):                          # resample one signal's pods onto the shared grid
        out = {}
        for pod, pts in per_pod.items():
            if pts[-1][0] < latest - 2 * step:        # stale/dead pod -> drop (no recent data)
                continue
            vec, j = np.full(n, np.nan), 0
            for k, gt in enumerate(grid):             # sample-and-hold onto the shared grid
                while j + 1 < len(pts) and pts[j + 1][0] <= gt + step / 2:
                    j += 1
                if abs(pts[j][0] - gt) <= step:
                    vec[k] = pts[j][1]
            if np.count_nonzero(~np.isnan(vec)) >= 12:  # real coverage; a gap == no activity == 0
                out[pod] = np.nan_to_num(vec, nan=0.0)
        return out

    vec_by_sig = {sig: to_vectors(raw[sig]) for sig in wanted}
    breach = sorted({e["pod"] for e in events if isinstance(e, dict) and e.get("kind") == "anomaly_candidate"})
    return vec_by_sig, breach


def _witness_for(signal, vectors):
    """Per-signal physical witness.
    - psi_io: disk (pvc) coupling among the storage quartet -> admits I/O cascade edges.
    - psi_cpu / psi_mem: same-node coupling (single node = one CPU/mem contention domain) ->
      admits a SOURCE-attributed edge only (the aggressor's usage leads a co-resident's stall, with
      NO network edge -- the S3 'mesh-blind' case). A bare psi pair still forms no edge (same-node
      is excluded from gate.couples), preserving the LOG-061 false-positive fix.
    psi co-pressure stays corroboration only."""
    pods = list(vectors)
    shared, copr, same_node = set(), set(), set()
    for i in range(len(pods)):
        for j in range(i + 1, len(pods)):
            a, b = pods[i], pods[j]
            if signal == "psi_io":
                if workload(a) in STORAGE and workload(b) in STORAGE:
                    shared.add(frozenset((a, b)))               # same physical disk (local-path)
            else:
                same_node.add(frozenset((a, b)))                # single node -> one CPU/mem domain (multi-node: scope by node label)
    hot = [p for p in pods if float(np.max(vectors[p][-6:])) > COPR_MIN]
    for i in range(len(hot)):
        for j in range(i + 1, len(hot)):
            copr.add(frozenset((hot[i], hot[j])))               # single node => same PSI domain (corroboration)
    return Witness(ebpf_edges=set(), psi_copressure=copr, shared_relation=shared, same_node=same_node)


def loop():
    global _graph
    while True:
        try:
            window = _fetch(WINDOW_URL)
            events = _fetch(EVENTS_URL)
            vec_by_sig, breach = build_inputs(window, events)
            rendered = {}                                  # {signal: rendered graph} for the merge
            for sig in SIGNALS:
                vectors = vec_by_sig.get(sig) or {}
                if not vectors:
                    continue
                mem = _memory[sig]
                witness = _witness_for(sig, vectors)
                src_sig = SIGNAL_SOURCES.get(sig)
                write_vectors = (vec_by_sig.get(src_sig) or None) if src_sig else None
                # per-pod incident threshold from the learned steady-state baseline (None while
                # still maturing) -> an onset is an incident only if it deviates from normal
                baselines = {pod: mem.baseline_threshold(workload(pod)) for pod in vectors}
                out = run_pass(vectors, witness, slo_breach=breach or None,
                               window=ANALYSIS_WINDOW, write_vectors=write_vectors,
                               baselines=baselines, recent=RESET_WINDOW)
                rendered[sig] = mem.observe(out, vectors, witness=witness, ts=time.time())
            merged = (merge_graphs(rendered, primary=PRIMARY) if rendered else
                      {"findings": [], "edges": [], "root_cause_ranking": [],
                       "blast_radius": [], "meta": {"signals": SIGNALS}})
            # OOM early-warning (A1 forecaster): project each pod's working_set ramp to its memory
            # limit. A leak is self-caused (no causal edge), so it rides as a separate `incipient`
            # list -- the "we told you before the kernel did" beat (S5), fired before the kill.
            mem_vec = vec_by_sig.get(FORECAST_SIGNAL) or {}
            lim_vec = vec_by_sig.get(FORECAST_LIMIT) or {}
            limits = {p: float(v[-1]) for p, v in lim_vec.items() if len(v) and v[-1] > 0}
            merged["incipient"] = incipient_findings(mem_vec, limits, horizon_s=FORECAST_HORIZON_S,
                                                     min_frac=FORECAST_MIN_FRAC)
            merged.setdefault("meta", {})["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with _lock:
                _graph = merged
        except Exception as e:  # never die; report the error on /graph
            with _lock:
                _graph = {"meta": {"status": "error", "error": str(e)}}
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n")
        if self.path.rstrip("/") in ("", "/graph"):
            with _lock:
                return self._send(200, json.dumps(_graph).encode(), "application/json")
        self._send(404, b"not found\n")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    threading.Thread(target=loop, daemon=True).start()
    print(f"correlation engine up on :{PORT}; window={WINDOW_URL} signals={','.join(SIGNALS)}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
