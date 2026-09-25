"""
Content-based fingerprinting for build artifacts.

Three-tier caching strategy:
  1. **mtime check** (fast): If source + cmd unchanged → return cached fingerprint
  2. **dep cache** (medium): If deps were scanned recently → skip g++ -MM
  3. **full scan** (slow): Run g++ -MM + hash all headers

All caches are persisted to disk between builds.
"""

import hashlib
import json
import os
import subprocess
import time
import concurrent.futures
from pathlib import Path

# In-memory + persistent cache: source_path -> {fp, mtime, cmd_hash, ts}
_GLOBAL_CACHE: dict[str, dict] = {}
_CACHE_FILE: str | None = None
_CACHE_DIR: str | None = None
_SAVE_COUNTER = 0
_SAVE_INTERVAL = 25

# Cached dependency lists: source_path -> {mtime, deps: [paths], ts}
_DEP_CACHE: dict[str, dict] = {}
_DEP_CACHE_FILE: str | None = None

# File hash cache: path -> {hash, mtime} to avoid re-hashing headers
_FILE_HASH_CACHE: dict[str, dict] = {}
_FILE_HASH_CACHE_FILE: str | None = None


def _ensure_cache(cache_dir: str):
    """Ensure persistent cache is initialized."""
    global _CACHE_FILE, _CACHE_DIR, _DEP_CACHE_FILE, _FILE_HASH_CACHE_FILE
    abs_dir = os.path.abspath(cache_dir)
    if _CACHE_DIR == abs_dir:
        return
    _CACHE_DIR = abs_dir
    _CACHE_FILE = os.path.join(_CACHE_DIR, "fingerprints.json")
    _DEP_CACHE_FILE = os.path.join(_CACHE_DIR, "deps.json")
    _FILE_HASH_CACHE_FILE = os.path.join(_CACHE_DIR, "file_hashes.json")
    os.makedirs(_CACHE_DIR, exist_ok=True)
    try:
        if os.path.isfile(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                data = json.load(f)
            now = time.time()
            _GLOBAL_CACHE.clear()
            _GLOBAL_CACHE.update({
                k: v for k, v in data.items()
                if os.path.isfile(k) and now - v.get("ts", 0) < 7 * 86400
            })
        if os.path.isfile(_DEP_CACHE_FILE):
            with open(_DEP_CACHE_FILE) as f:
                deps_data = json.load(f)
            now = time.time()
            _DEP_CACHE.clear()
            _DEP_CACHE.update({
                k: v for k, v in deps_data.items()
                if os.path.isfile(k) and now - v.get("ts", 0) < 7 * 86400
            })
        if os.path.isfile(_FILE_HASH_CACHE_FILE):
            with open(_FILE_HASH_CACHE_FILE) as f:
                hash_data = json.load(f)
            now = time.time()
            _FILE_HASH_CACHE.clear()
            _FILE_HASH_CACHE.update({
                k: v for k, v in hash_data.items()
                if os.path.isfile(k) and now - v.get("ts", 0) < 7 * 86400
            })
    except Exception:
        pass


def _flush_cache():
    """Flush all caches to disk."""
    global _SAVE_COUNTER
    _SAVE_COUNTER += 1
    if _SAVE_COUNTER < _SAVE_INTERVAL:
        return
    _SAVE_COUNTER = 0
    try:
        if _CACHE_FILE:
            tmp_fp = _CACHE_FILE + ".tmp"
            with open(tmp_fp, "w") as f:
                json.dump(_GLOBAL_CACHE, f)
            os.replace(tmp_fp, _CACHE_FILE)
        if _DEP_CACHE_FILE:
            tmp_dp = _DEP_CACHE_FILE + ".tmp"
            with open(tmp_dp, "w") as f:
                json.dump(_DEP_CACHE, f)
            os.replace(tmp_dp, _DEP_CACHE_FILE)
        if _FILE_HASH_CACHE_FILE:
            tmp_hp = _FILE_HASH_CACHE_FILE + ".tmp"
            with open(tmp_hp, "w") as f:
                json.dump(_FILE_HASH_CACHE, f)
            os.replace(tmp_hp, _FILE_HASH_CACHE_FILE)
    except Exception:
        pass


def _sha256_file(path: str) -> bytes:
    """SHA-256 of a file, using cached hash if available."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return b"\x00" * 32
    
    cached = _FILE_HASH_CACHE.get(path)
    if cached and cached.get("mtime") == mtime:
        return bytes.fromhex(cached["hash"])
    
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(131072), b""):
            h.update(chunk)
    result = h.digest()
    
    # Cache the hash
    _FILE_HASH_CACHE[path] = {
        "hash": result.hex(),
        "mtime": mtime,
        "ts": time.time(),
    }
    return result


def _cmd_hash(cmd_line: list[str]) -> str:
    """Quick hash of the command line."""
    return hashlib.md5(" ".join(cmd_line).encode()).hexdigest()


def _scan_one(source: str, include_dirs: list[str]) -> tuple[str, list[str]]:
    """Scan includes for a single file (thread-safe)."""
    deps = scan_includes(source, include_dirs)
    return (source, deps)


def scan_includes(source: str, include_dirs: list[str]) -> list[str]:
    """
    Return the list of resolved header paths that *source* transitively
    depends on, using gcc's preprocessor in pre-compute mode.
    """
    cmd = ["g++", "-MM", "-MG", "-std=c++20"]
    for d in include_dirs:
        cmd.extend(["-I", d])
    cmd.append(source)

    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=30
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    deps: list[str] = []
    for line in out.decode().splitlines():
        for part in line.replace("\\", "").strip().split():
            if part.endswith((".h", ".hpp", ".hh")) and os.path.isabs(part):
                deps.append(part)
    result = sorted(set(deps))
    return result


def pre_scan_all(sources: list[str], include_dirs: list[str],
                 cache_dir: str = "xb-cache", parallel: bool = True):
    """
    Pre-scan all source files for dependencies.

    Only scans files that have changed since last scan.
    """
    _ensure_cache(cache_dir)
    
    # Normalize all paths to absolute
    abs_sources = [os.path.abspath(s) for s in sources]
    
    # Filter out sources already cached
    needs_scan = []
    for src in abs_sources:
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            continue
        cached = _DEP_CACHE.get(src)
        if cached and cached.get("mtime") == mtime:
            continue
        needs_scan.append(src)
    
    if not needs_scan:
        return {}
    
    if parallel and len(needs_scan) > 8:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(18, len(needs_scan))
        ) as pool:
            results = list(pool.map(
                lambda s: _scan_one(s, include_dirs),
                needs_scan
            ))
        
        for src, deps in results:
            try:
                mtime = os.path.getmtime(src)
            except OSError:
                continue
            _DEP_CACHE[src] = {"mtime": mtime, "deps": deps, "ts": time.time()}
            _flush_cache()
    else:
        for src in needs_scan:
            deps = scan_includes(src, include_dirs)
            try:
                mtime = os.path.getmtime(src)
            except OSError:
                continue
            _DEP_CACHE[src] = {"mtime": mtime, "deps": deps, "ts": time.time()}
            _flush_cache()


def compute_fingerprint(
    source: str,
    cmd_line: list[str],
    include_dirs: list[str],
    cache_dir: str = "xb-cache",
    scan_deps: bool = True,
) -> str:
    """
    Compute the build fingerprint for a source file.
    """
    _ensure_cache(cache_dir)

    # Normalize to absolute path for cache lookup
    source = os.path.abspath(source)

    cmd_h = _cmd_hash(cmd_line)
    try:
        mtime = os.path.getmtime(source)
    except OSError:
        mtime = 0

    # Fast path: mtime + cmd unchanged
    entry = _GLOBAL_CACHE.get(source)
    if entry and entry.get("mtime") == mtime and entry.get("cmd_hash") == cmd_h:
        return entry["fp"]

    # Recompute fingerprint
    h = hashlib.sha256()
    h.update(_sha256_file(source))
    for arg in cmd_line:
        h.update(arg.encode())
        h.update(b"\x00")

    if scan_deps:
        # Try dep cache first
        dep_entry = _DEP_CACHE.get(source)
        if dep_entry and dep_entry.get("mtime") == mtime:
            deps = dep_entry["deps"]
        else:
            deps = scan_includes(source, include_dirs)
            _DEP_CACHE[source] = {"mtime": mtime, "deps": deps, "ts": time.time()}

        for dep in deps:
            try:
                h.update(_sha256_file(dep))
            except OSError:
                pass

    fp = h.hexdigest()
    _GLOBAL_CACHE[source] = {
        "fp": fp,
        "mtime": mtime,
        "cmd_hash": cmd_h,
        "ts": time.time(),
    }
    _flush_cache()
    return fp
