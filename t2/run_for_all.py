"""Process every tile from data/tiles.json end-to-end (ingest -> fetch -> conflate -> checks).

Canonical entry point: ``python -m t2.run_for_all [--workers N]``.

Coordination state lives under ``data/``:

    run_for_all_status.json   per-tile state ({pending,running,done,skipped,error,cancelled})
    run_for_all.lock          PID of the running parent (present only while running)
    run_for_all.stop          stop-request flag (touched by the UI; checked between dispatches)
    run_for_all.log           stdout+stderr of the last worker invocation

Tiles are processed in parallel via ``ProcessPoolExecutor``. Workers push
progress events into a Manager queue; the parent process is the sole writer
of the status file (so the UI sees a consistent snapshot).

Deterministic run names (``{tile_id}-batch-{YYYYMMDD}``) make re-clicking
"Run for All" resume rather than duplicate: tiles whose runs already have
all four stages green are marked ``skipped`` and the worker returns fast.

Each worker is a separate Python process with its own ``osm_fetch`` cache —
the shared toronto-addresses.json is parsed once per worker, then reused for
every tile that worker handles. Memory cost: ~one parsed copy of the extract
per worker.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import os
import queue as _queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as _config, pipeline


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(cfg=None) -> dict[str, Path]:
    cfg = cfg or _config.load()
    d = cfg.data_dir
    return {
        "status": d / "run_for_all_status.json",
        "lock": d / "run_for_all.lock",
        "stop": d / "run_for_all.stop",
        "log": d / "run_for_all.log",
        "tiles": d / "tiles.json",
    }


def status_path(cfg=None) -> Path:
    return _paths(cfg)["status"]


def lock_path(cfg=None) -> Path:
    return _paths(cfg)["lock"]


def stop_path(cfg=None) -> Path:
    return _paths(cfg)["stop"]


def log_path(cfg=None) -> Path:
    return _paths(cfg)["log"]


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 1) // 2))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running(cfg=None) -> tuple[bool, int | None]:
    """Return (is_running, pid). A lock with a dead PID is treated as not running."""
    p = lock_path(cfg)
    if not p.exists():
        return False, None
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return False, None
    if _pid_alive(pid):
        return True, pid
    return False, pid


def read_status(cfg=None) -> dict | None:
    p = status_path(cfg)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def request_stop(cfg=None) -> None:
    stop_path(cfg).write_text(_iso_now(), encoding="utf-8")


def reset_state(cfg=None) -> None:
    """Delete the status file and stop flag. Does not touch runs or DB."""
    for key in ("status", "stop"):
        p = _paths(cfg)[key]
        p.unlink(missing_ok=True)


def _write_status(path: Path, status: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    tmp.replace(path)


def _stage_status_complete(run_id: int) -> bool:
    s = pipeline.stage_status(run_id)
    return all(s.get(k) for k in ("ingest", "fetch", "conflate", "checks"))


# ---- Worker (runs in a child process) -------------------------------------

# Module-level so the function is picklable for spawn-based pools on Windows.
def _run_tile_worker(tile: dict, msg_queue) -> dict:
    tid = tile["id"]
    bbox = tuple(tile["bbox"])

    def _push(event: str, **fields) -> None:
        msg_queue.put({"event": event, "tile_id": tid, "ts": _iso_now(), **fields})

    try:
        name = f"{tid}-batch-{datetime.now().date().isoformat()}"
        run_id = pipeline.start_run(name, bbox)  # type: ignore[arg-type]
        _push("tile_start", run_id=run_id)

        if _stage_status_complete(run_id):
            _push("tile_done", run_id=run_id, state="skipped", stage="checks")
            return {"tile_id": tid, "state": "skipped", "run_id": run_id}

        stages = (
            ("ingest", pipeline.ingest_stage),
            ("fetch", pipeline.fetch_stage),
            ("conflate", pipeline.conflate_stage),
            ("checks", pipeline.run_checks),
        )
        for stage_name, fn in stages:
            _push("stage", run_id=run_id, stage=stage_name)
            fn(run_id)

        _push("tile_done", run_id=run_id, state="done", stage="checks")
        return {"tile_id": tid, "state": "done", "run_id": run_id}
    except Exception as exc:
        _push("tile_done", state="error", error=f"{type(exc).__name__}: {exc}")
        return {"tile_id": tid, "state": "error", "error": str(exc)}


# ---- Parent: event application + main loop --------------------------------

def _apply_event(status: dict, msg: dict) -> None:
    tid = msg.get("tile_id")
    if not tid:
        return
    entry = status["tiles"].setdefault(tid, {})
    event = msg.get("event")
    ts = msg.get("ts") or _iso_now()
    if event == "tile_start":
        entry["state"] = "running"
        entry["run_id"] = msg.get("run_id")
        entry["error"] = None
        entry["stage"] = None
        entry["updated_at"] = ts
    elif event == "stage":
        entry["state"] = "running"
        entry["stage"] = msg.get("stage")
        if msg.get("run_id"):
            entry["run_id"] = msg["run_id"]
        entry["updated_at"] = ts
    elif event == "tile_done":
        entry["state"] = msg.get("state", "done")
        if msg.get("stage"):
            entry["stage"] = msg["stage"]
        if msg.get("error"):
            entry["error"] = msg["error"]
        if msg.get("run_id") and not entry.get("run_id"):
            entry["run_id"] = msg["run_id"]
        entry["updated_at"] = ts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Process every tile end-to-end in parallel.")
    p.add_argument("--workers", type=int, default=default_workers(),
                   help=f"Number of worker processes (default: {default_workers()})")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only the first N tiles (0 = all). Useful for debugging.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = _config.load()
    paths = _paths(cfg)
    paths["status"].parent.mkdir(parents=True, exist_ok=True)

    paths["stop"].unlink(missing_ok=True)
    paths["lock"].write_text(str(os.getpid()), encoding="utf-8")
    try:
        if not paths["tiles"].exists():
            print(f"[run_for_all] tiles file not found: {paths['tiles']}", flush=True)
            return 1
        tiles_doc = json.loads(paths["tiles"].read_text(encoding="utf-8"))
        tiles: list[dict] = tiles_doc.get("tiles", [])
        if args.limit and args.limit > 0:
            tiles = tiles[: args.limit]
        if not tiles:
            print("[run_for_all] no tiles in tiles.json", flush=True)
            return 1

        n_workers = max(1, args.workers)
        status = {
            "started_at": _iso_now(),
            "finished_at": None,
            "stopped": False,
            "total": len(tiles),
            "workers": n_workers,
            "tiles": {},
        }
        _write_status(paths["status"], status)
        print(f"[run_for_all] starting {n_workers} worker(s) over {len(tiles)} tile(s)", flush=True)

        with multiprocessing.Manager() as mgr:
            msg_queue = mgr.Queue()
            with concurrent.futures.ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures: dict[concurrent.futures.Future, str] = {
                    ex.submit(_run_tile_worker, t, msg_queue): t["id"] for t in tiles
                }
                stopped_pending = False
                last_write = 0.0

                while futures:
                    # Drain queue events from workers.
                    drained = 0
                    while True:
                        try:
                            msg = msg_queue.get_nowait()
                        except _queue.Empty:
                            break
                        _apply_event(status, msg)
                        drained += 1

                    # Stop flag: cancel still-pending futures, let in-flight finish.
                    if paths["stop"].exists() and not stopped_pending:
                        cancelled = 0
                        for f in list(futures):
                            if f.cancel():
                                tid = futures.pop(f)
                                entry = status["tiles"].setdefault(tid, {})
                                entry["state"] = "cancelled"
                                entry["updated_at"] = _iso_now()
                                cancelled += 1
                        status["stopped"] = True
                        stopped_pending = True
                        print(f"[run_for_all] stop requested; cancelled {cancelled} pending tile(s)", flush=True)

                    # Reap completed futures.
                    done_futures = [f for f in futures if f.done()]
                    for f in done_futures:
                        tid = futures.pop(f)
                        try:
                            f.result()
                        except concurrent.futures.CancelledError:
                            pass
                        except Exception as exc:
                            entry = status["tiles"].setdefault(tid, {})
                            if entry.get("state") not in ("error", "done", "skipped"):
                                entry["state"] = "error"
                                entry["error"] = f"{type(exc).__name__}: {exc}"
                                entry["updated_at"] = _iso_now()

                    now = time.monotonic()
                    if drained or done_futures or now - last_write >= 1.0:
                        _write_status(paths["status"], status)
                        last_write = now

                    if futures and not done_futures and not drained:
                        time.sleep(0.1)

            # Drain any final events still sitting in the queue post-shutdown.
            while True:
                try:
                    msg = msg_queue.get_nowait()
                except _queue.Empty:
                    break
                _apply_event(status, msg)

        status["finished_at"] = _iso_now()
        _write_status(paths["status"], status)
        return 0
    finally:
        paths["lock"].unlink(missing_ok=True)
        paths["stop"].unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
