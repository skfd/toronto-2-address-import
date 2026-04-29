"""Process every tile from data/tiles.json end-to-end (ingest -> fetch -> conflate -> checks).

Canonical entry point: ``python -m t2.run_for_all``.

Coordination state lives under ``data/``:

    run_for_all_status.json   per-tile state ({pending,running,done,error}, run_id, stage, error)
    run_for_all.lock          PID of the running worker (present only while running)
    run_for_all.stop          stop-request flag (touched by the UI; worker checks each tile)
    run_for_all.log           stdout+stderr of the last worker invocation

The worker is sequential by design — the tool DB is one SQLite file with WAL,
so concurrent stage writes against the same DB would just serialize and
contend on commits.

Deterministic run names (``{tile_id}-batch-{YYYYMMDD}``) make re-clicking
"Run for All" resume rather than duplicate: tiles whose runs already have all
four stages green are skipped.
"""
from __future__ import annotations

import json
import os
import sys
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


def _run_for_tile(tile: dict) -> int:
    """Resolve a deterministic run for this tile: reopen if it already exists."""
    bbox = tuple(tile["bbox"])
    name = f"{tile['id']}-batch-{datetime.now().date().isoformat()}"
    return pipeline.start_run(name, bbox)  # type: ignore[arg-type]


def _process_tile(tile: dict, status: dict, status_file: Path) -> None:
    tid = tile["id"]
    entry: dict[str, Any] = status["tiles"].setdefault(tid, {})
    entry["state"] = "running"
    entry["error"] = None
    entry["updated_at"] = _iso_now()
    status["current_tile"] = tid
    _write_status(status_file, status)

    run_id = _run_for_tile(tile)
    entry["run_id"] = run_id

    if _stage_status_complete(run_id):
        entry["state"] = "skipped"
        entry["stage"] = "checks"
        entry["updated_at"] = _iso_now()
        _write_status(status_file, status)
        return

    for stage in ("ingest", "fetch", "conflate", "checks"):
        entry["stage"] = stage
        entry["updated_at"] = _iso_now()
        status["current_stage"] = stage
        _write_status(status_file, status)
        runner = {
            "ingest": pipeline.ingest_stage,
            "fetch": pipeline.fetch_stage,
            "conflate": pipeline.conflate_stage,
            "checks": pipeline.run_checks,
        }[stage]
        runner(run_id)

    entry["state"] = "done"
    entry["stage"] = "checks"
    entry["updated_at"] = _iso_now()
    _write_status(status_file, status)


def main() -> int:
    cfg = _config.load()
    paths = _paths(cfg)
    paths["status"].parent.mkdir(parents=True, exist_ok=True)

    # Clear any lingering stop flag from a prior run.
    paths["stop"].unlink(missing_ok=True)

    paths["lock"].write_text(str(os.getpid()), encoding="utf-8")
    try:
        if not paths["tiles"].exists():
            print(f"[run_for_all] tiles file not found: {paths['tiles']}", flush=True)
            return 1
        tiles_doc = json.loads(paths["tiles"].read_text(encoding="utf-8"))
        tiles: list[dict] = tiles_doc.get("tiles", [])
        if not tiles:
            print("[run_for_all] no tiles in tiles.json", flush=True)
            return 1

        status = {
            "started_at": _iso_now(),
            "finished_at": None,
            "stopped": False,
            "total": len(tiles),
            "current_tile": None,
            "current_stage": None,
            "tiles": {},
        }
        _write_status(paths["status"], status)

        for tile in tiles:
            if paths["stop"].exists():
                status["stopped"] = True
                break
            tid = tile["id"]
            print(f"[run_for_all] tile {tid}", flush=True)
            try:
                _process_tile(tile, status, paths["status"])
            except Exception as exc:
                entry = status["tiles"].setdefault(tid, {})
                entry["state"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["updated_at"] = _iso_now()
                _write_status(paths["status"], status)
                print(f"[run_for_all] tile {tid} failed: {exc}", flush=True)

        status["finished_at"] = _iso_now()
        status["current_tile"] = None
        status["current_stage"] = None
        _write_status(paths["status"], status)
        return 0
    finally:
        paths["lock"].unlink(missing_ok=True)
        paths["stop"].unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
