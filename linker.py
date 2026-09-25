"""
Linker module – creates static archives and final executable.
"""

import logging
import os
import subprocess
from pathlib import Path

from .target import BuildGraph, Module

log = logging.getLogger("xb.linker")


def archive_module(mod: Module) -> bool:
    """Create static archive (.a) from a module's object files."""
    if not mod.targets:
        log.warning(f"Module {mod.name} has no targets – skipping archive")
        return True

    objs = [t.obj for t in mod.targets if os.path.isfile(t.obj)]
    if not objs:
        log.error(f"No object files found for module {mod.name}")
        return False

    os.makedirs(os.path.dirname(mod.archive) or ".", exist_ok=True)

    cmd = ["ar", "rcs", mod.archive] + objs
    log.info(f"Linking {len(objs)} objects → {mod.archive}")

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0:
            log.error(f"ar failed: {proc.stderr.decode(errors='replace')[:500]}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"ar timed out for {mod.name}")
        return False

    mod.status = "done"
    mod.needs_link = False
    return True


def link_modules(graph: BuildGraph) -> bool:
    """Create static archives for all modules."""
    all_ok = True
    for mod_name in graph.topo_sort():
        mod = graph.modules.get(mod_name)
        if mod and mod.needs_link:
            if not archive_module(mod):
                all_ok = False
    return all_ok


def _classify_libs(deps) -> tuple[list[str], list[str]]:
    """
    Separate deps.libs into static archive paths and shared lib names.
    
    For each lib name, check if a .a file exists in any lib_dir.
    If so, return the full path to the .a. Otherwise return the
    lib name for -l linking.
    """
    static_paths = []
    shared_libs = []

    # Only check directories that actually exist
    lib_dirs = [d for d in deps.lib_dirs if os.path.isdir(d)]

    for lib_name in deps.libs:
        # Skip if already handled
        if lib_name in shared_libs:
            continue
        # Look for a static .a file
        found = False
        for lib_dir in lib_dirs:
            static_file = os.path.join(lib_dir, f"lib{lib_name}.a")
            if os.path.isfile(static_file):
                static_paths.append(static_file)
                found = True
                break
        if not found:
            shared_libs.append(lib_name)

    return static_paths, shared_libs


def link_executable(
    graph: BuildGraph,
    cc: str = "g++",
    cxxflags: list[str] | None = None,
    ldflags: list[str] | None = None,
    libs: list[str] | None = None,
    deps=None,
) -> bool:
    """
    Link the final executable from all module archives.
    
    Static archives go in --start-group/--end-group.
    Shared libraries go after the group so the linker resolves
    symbols from static archives against them.
    """
    exe = graph.executable
    if not exe:
        log.info("No executable target – skipping link")
        return True

    # Collect our project archives in dependency order
    archives = []
    for mod_name in graph.topo_sort():
        mod = graph.modules.get(mod_name)
        if mod and os.path.isfile(mod.archive):
            archives.append(mod.archive)

    if not archives:
        log.error("No archives to link")
        return False

    # Classify dependency libs
    static_paths = []
    shared_libs = []
    static_lib_names = set()
    if deps:
        static_paths, shared_libs = _classify_libs(deps)
        static_lib_names = {os.path.basename(p)[3:-2] for p in static_paths}  # extract lib names
        log.info(f"Classified {len(static_paths)} static, {len(shared_libs)} shared libs")

    # Additional system libs from exe.libs - only add if not already static or shared
    for lib in list(exe.libs) + (libs or []):
        if lib not in shared_libs and lib not in static_lib_names:
            shared_libs.append(lib)

    # Build link command
    cmd = [cc]
    cmd.extend(cxxflags or [])
    cmd.extend(exe.ldflags)
    cmd.extend(ldflags or [])

    # Add -L flags for lib dirs
    if deps:
        for d in deps.lib_dirs:
            if os.path.isdir(d):
                cmd.extend(['-L', d])

    # --start-group wraps our archives + conan/static deps
    cmd.append("-Wl,--start-group")
    cmd.extend(archives)
    cmd.extend(static_paths)
    cmd.append("-Wl,--end-group")

    # Shared libraries after the group
    for lib in shared_libs:
        cmd.extend(["-l", lib])

    # RPATH - add lib_dirs so the runtime can find shared libs
    if deps:
        rpath_dirs = [d for d in deps.lib_dirs if os.path.isdir(d)]
        if rpath_dirs:
            cmd.extend(['-Wl,-rpath,' + d for d in rpath_dirs])

    cmd.extend(["-o", exe.output])

    os.makedirs(os.path.dirname(exe.output) or ".", exist_ok=True)

    log.info(f"Linking executable: {exe.output}")
    log.debug(f"Link command: {' '.join(cmd[:40])}...")

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")
            log.error(f"Link failed: {stderr[:1000]}")
            return False
    except subprocess.TimeoutExpired:
        log.error("Link timed out")
        return False

    exe.status = "done"
    log.info(f"Linked {exe.output} ({os.path.getsize(exe.output) / 1024 / 1024:.1f} MB)")
    return True


def link(graph: BuildGraph, cc: str = "g++",
         cxxflags: list[str] | None = None,
         ldflags: list[str] | None = None,
         libs: list[str] | None = None,
         deps=None) -> bool:
    """Full link step: archive all modules, then link executable."""
    ok1 = link_modules(graph)
    if not ok1:
        return False
    ok2 = link_executable(graph, cc, cxxflags, ldflags, libs, deps)
    return ok2
