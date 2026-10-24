#!/usr/bin/env python3
"""
Lightweight resource monitor for the LiveKit stack.

Tracks CPU / memory per service, aggregating each service's whole process tree
(important for the agent, which spawns multiprocessing worker forks).

Usage:
    python monitor.py                 # live refreshing table (default 2s)
    python monitor.py --interval 1    # refresh every second
    python monitor.py --once          # single snapshot, then exit
    python monitor.py --csv usage.csv # also append every sample to a CSV

Run with the project venv (ensure psutil is installed there):
    ../.venv/bin/python monitor.py
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    sys.exit(
        "psutil not found. Install it into the project venv, e.g.:\n"
        "  ../.venv/bin/pip install psutil && ../.venv/bin/python monitor.py"
    )

# Each service: a label and a predicate matching its ROOT process by cmdline.
# Children (e.g. the agent's multiprocessing forks) are picked up via the tree.
SERVICES = [
    ("livekit-server", lambda c, n: "livekit-server" in n or "livekit-server" in c),
    ("redis",          lambda c, n: n == "redis-server" or "redis-server" in c),
    # This project's agent is agent.py (livekit-agents worker), not worker_main.py.
    ("agent",          lambda c, n: "agent.py" in c and "dispatcher" not in c),
    ("dispatcher",     lambda c, n: "local_dispatcher.py" in c),
    ("web (token)",    lambda c, n: "livekit-web" in c and "token_server.py" in c),
]

NCPU = psutil.cpu_count() or 1
_proc_cache: dict[int, psutil.Process] = {}


def _get_proc(pid: int) -> psutil.Process:
    """Cache Process objects so cpu_percent() measures the delta since last call."""
    p = _proc_cache.get(pid)
    if p is None or not p.is_running():
        p = psutil.Process(pid)
        p.cpu_percent(None)  # prime
        _proc_cache[pid] = p
    return p


def find_roots() -> dict[str, list[psutil.Process]]:
    """Map each service label to the list of root processes matching it."""
    roots = {label: [] for label, _ in SERVICES}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for label, match in SERVICES:
            if match(cmd, name):
                roots[label].append(proc)
                break  # a process belongs to at most one service
    return roots


def sample_service(roots: list[psutil.Process]) -> dict:
    """Aggregate CPU%, RSS, process and thread counts across each root's tree."""
    pids: dict[int, psutil.Process] = {}
    for root in roots:
        try:
            pids[root.pid] = root
            for child in root.children(recursive=True):
                pids[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    cpu = rss = threads = 0.0
    nproc = 0
    oldest = None
    for pid in pids:
        try:
            p = _get_proc(pid)
            cpu += p.cpu_percent(None)
            rss += p.memory_info().rss
            threads += p.num_threads()
            nproc += 1
            ct = p.create_time()
            oldest = ct if oldest is None else min(oldest, ct)
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            _proc_cache.pop(pid, None)
            continue

    uptime = "" if oldest is None else _fmt_uptime(time.time() - oldest)
    return {
        "procs": nproc,
        "threads": int(threads),
        "cpu": cpu,                      # summed across cores (can exceed 100%)
        "rss_mb": rss / (1024 * 1024),
        "uptime": uptime,
    }


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def render(rows: list[tuple[str, dict]]) -> str:
    sys_cpu = psutil.cpu_percent(None)
    vm = psutil.virtual_memory()
    hdr = (
        f"LiveKit stack monitor  —  {datetime.now():%H:%M:%S}   "
        f"host CPU {sys_cpu:5.1f}% ({NCPU} cores)   "
        f"host MEM {vm.percent:4.1f}% "
        f"({vm.used/1e9:.1f}/{vm.total/1e9:.1f} GB)\n"
    )
    line = "-" * 78 + "\n"
    out = [hdr, line]
    out.append(f"{'SERVICE':<16}{'STATUS':<9}{'PROC':>5}{'THR':>5}{'CPU%':>8}{'MEM(MB)':>10}{'UPTIME':>10}\n")
    out.append(line)
    tot_cpu = tot_rss = tot_proc = 0.0
    for label, s in rows:
        if s["procs"] == 0:
            out.append(f"{label:<16}{'DOWN':<9}{'-':>5}{'-':>5}{'-':>8}{'-':>10}{'-':>10}\n")
            continue
        tot_cpu += s["cpu"]; tot_rss += s["rss_mb"]; tot_proc += s["procs"]
        out.append(
            f"{label:<16}{'up':<9}{s['procs']:>5}{s['threads']:>5}"
            f"{s['cpu']:>8.1f}{s['rss_mb']:>10.1f}{s['uptime']:>10}\n"
        )
    out.append(line)
    out.append(f"{'TOTAL':<16}{'':<9}{int(tot_proc):>5}{'':>5}{tot_cpu:>8.1f}{tot_rss:>10.1f}\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="LiveKit stack resource monitor")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("--once", action="store_true", help="single snapshot then exit")
    ap.add_argument("--csv", metavar="FILE", help="append each sample to a CSV file")
    args = ap.parse_args()

    csv_writer = None
    csv_file = None
    if args.csv:
        new = not os.path.exists(args.csv)
        csv_file = open(args.csv, "a", newline="")
        csv_writer = csv.writer(csv_file)
        if new:
            csv_writer.writerow(["timestamp", "service", "procs", "threads", "cpu_pct", "rss_mb"])

    # Prime CPU counters (host + per-process), then measure after a short delay
    # so the very first sample shows real CPU% rather than 0.
    psutil.cpu_percent(None)
    roots = find_roots()
    for label, _ in SERVICES:
        sample_service(roots.get(label, []))
    time.sleep(min(args.interval, 1.0))

    try:
        while True:
            roots = find_roots()
            rows = [(label, sample_service(roots.get(label, []))) for label, _ in SERVICES]

            if csv_writer:
                ts = datetime.now().isoformat(timespec="seconds")
                for label, s in rows:
                    csv_writer.writerow([ts, label, s["procs"], s["threads"],
                                         f"{s['cpu']:.1f}", f"{s['rss_mb']:.1f}"])
                csv_file.flush()

            if args.once:
                sys.stdout.write(render(rows))
                break

            sys.stdout.write("\033[2J\033[H")  # clear screen, home cursor
            sys.stdout.write(render(rows))
            sys.stdout.write("\n(Ctrl-C to quit)\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
