# LiveKit stack monitor

Lightweight CPU/memory monitor for the whole stack. Each service is aggregated
across its **entire process tree**, so the agent's multiprocessing worker forks
roll up into a single `agent` row.

Tracks: `livekit-server`, `redis`, `agent`, `dispatcher`, `web (token)`.

## Run

Use the agent venv (it already has `psutil`):

```bash
cd livekit-monitor
PY=../.venv/bin/python

$PY monitor.py                 # live refreshing table (2s)
$PY monitor.py --interval 1    # refresh every second
$PY monitor.py --once          # one snapshot, then exit
$PY monitor.py --csv usage.csv # also append every sample to CSV (great for graphing later)
```

## Columns

| Column   | Meaning |
|----------|---------|
| PROC     | processes in the service's tree |
| THR      | total threads |
| CPU%     | summed across cores — can exceed 100% (16 cores here) |
| MEM(MB)  | resident memory (RSS) summed over the tree |
| UPTIME   | age of the oldest process in the tree |

A service shows `DOWN` if no matching process is found.

## Notes

- CPU% is a delta since the previous sample, so the first reading takes one
  interval to become meaningful (handled automatically).
- Matching is by command line (`worker_main.py`, `local_dispatcher.py`,
  `token_server.py`, `livekit-server`, `redis-server`). If you rename a launch
  command, update the `SERVICES` table in `monitor.py`.
