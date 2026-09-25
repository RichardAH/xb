"""
Parallel compile dispatch engine.

Uses a bounded thread pool with a work queue.  Each worker thread:
  1. Picks the next pending target from the queue
  2. Checks fingerprint against cached version (read from sidecar .fp file)
  3. Compiles (skipping if fingerprint matches and .o exists)
  4. Writes the new fingerprint to the sidecar .fp file
  5. Updates status and yields to the next target
"""

import concurrent.futures
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .fingerprint import compute_fingerprint
from .target import BuildGraph, Target

log = logging.getLogger("xb.compiler")

# Thread-safe progress counter
_progress_lock = threading.Lock()
_progress = {"done": 0, "failed": 0, "skipped": 0, "started": 0.0}


def _read_fp_file(obj_path: str) -> str | None:
    """Read cached fingerprint from sidecar .fp file."""
    fp_path = obj_path + ".fp"
    try:
        with open(fp_path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_fp_file(obj_path: str, fp: str):
    """Write fingerprint to sidecar .fp file."""
    fp_path = obj_path + ".fp"
    try:
        with open(fp_path, "w") as f:
            f.write(fp)
    except OSError:
        pass


def _compile_one(target: Target, include_dirs: list[str], cache_dir: str,
                 all_have_fp: bool) -> bool:
    """Compile a single target. Returns True on success."""
    cmd = target.cmd
    if not cmd:
        log.error(f"No command line for {target.src}")
        return False

    # Check if we can skip compilation
    if os.path.isfile(target.obj):
        cached_fp = _read_fp_file(target.obj)
        if cached_fp:
            current_fp = compute_fingerprint(
                target.src, cmd, include_dirs,
                cache_dir=cache_dir, scan_deps=all_have_fp
            )
            if current_fp == cached_fp:
                target.fp = cached_fp
                target.status = "done"
                with _progress_lock:
                    _progress["skipped"] += 1
                return True
        elif not all_have_fp:
            # Cold build: .o exists but no .fp file (ccache served it)
            # Recompile with ccache (instant) and generate new .fp
            pass  # fall through to compile

    # Recompile
    current_fp = compute_fingerprint(
        target.src, cmd, include_dirs,
        cache_dir=cache_dir, scan_deps=all_have_fp
    )

    # Ensure output directory exists
    os.makedirs(os.path.dirname(target.obj) or ".", exist_ok=True)

    # Run compilation (ccache will serve from cache for cold builds)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=900,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")[:2000]
            log.error(f"FAIL {target.src}: {stderr[:500]}")
            target.status = "failed"
            with _progress_lock:
                _progress["failed"] += 1
            return False
    except subprocess.TimeoutExpired:
        log.error(f"TIMEOUT {target.src}")
        target.status = "failed"
        with _progress_lock:
            _progress["failed"] += 1
        return False
    except Exception as e:
        log.error(f"ERROR compiling {target.src}: {e}")
        target.status = "failed"
        with _progress_lock:
            _progress["failed"] += 1
        return False

    # Store fingerprint
    target.fp = current_fp
    _write_fp_file(target.obj, current_fp)
    target.status = "done"

    with _progress_lock:
        _progress["done"] += 1

    return True


def dispatch(
    graph: BuildGraph,
    include_dirs: list[str],
    jobs: int | None = None,
    progress_cb=None,
    all_have_fp: bool = False,
) -> dict:
    """
    Dispatch all pending targets to worker threads.

    all_have_fp: If True, use full fingerprint checks (incremental build).
                 If False, trust ccache for cold builds (skip dep scan).
    """
    if jobs is None:
        jobs = os.cpu_count() or 4

    targets = graph.ordered_targets()
    total = len(targets)
    if total == 0:
        return {"compiled": 0, "skipped": 0, "failed": 0, "elapsed": 0.0}

    mode = "incremental" if all_have_fp else "cold"
    log.info(f"Dispatching {total} targets to {jobs} workers (mode={mode})")
    _progress["started"] = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = []
        for target in targets:
            if target.status == "pending":
                future = pool.submit(
                    _compile_one, target, include_dirs, graph.cache_dir,
                    all_have_fp
                )
                futures.append((future, target))

        done = 0
        for future, target in futures:
            try:
                future.result(timeout=1200)
            except Exception as e:
                log.error(f"Worker exception for {target.src}: {e}")
                target.status = "failed"
                with _progress_lock:
                    _progress["failed"] += 1
            done += 1
            if progress_cb:
                progress_cb(done, total, target)

    elapsed = time.monotonic() - _progress["started"]

    result = {
        "compiled": _progress["done"],
        "skipped": _progress["skipped"],
        "failed": _progress["failed"],
        "total": total,
        "elapsed": elapsed,
        "jobs": jobs,
    }

    log.info(
        f"Compile done: {result['compiled']} built, "
        f"{result['skipped']} skipped, "
        f"{result['failed']} failed, "
        f"{elapsed:.1f}s"
    )
    return result
