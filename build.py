"""
xb build orchestrator – main entry point.

Usage:
    python -m xb build [--jobs N] [--type Release|Debug] [--clean]
    python -m xb info
    python -m xb clean
"""

import argparse
import glob
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .project import load_xbp, discover_modules
from .toolchain import Toolchain
from .target import BuildGraph
from .compiler import dispatch
from .linker import link_modules, link_executable, archive_module
from .cache import BuildCache
from .deps import ExternalDeps, generate_protobuf, find_proto_sources, ensure_proto_files
from .sysdeps import check_all_deps


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _is_cold_build(build_dir: str) -> bool:
    """
    Check if this is a cold build (no existing .o files).
    
    On cold builds we can skip the fingerprint pre-scan entirely
    since nothing will be reused anyway.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["find", build_dir, "-name", "*.o", "-type", "f", "-print", "-quit"],
            capture_output=True, timeout=5
        )
        return result.stdout.strip() == ""  # cold if no .o files found
    except Exception:
        return True  # assume cold if check fails


def build_project(
    src_root: str = ".",
    build_dir: str = "xb-build",
    cache_dir: str = "xb-cache",
    build_type: str = "Release",
    jobs: int | None = None,
    clean: bool = False,
    conan_build: str | None = None,
    skip_proto: bool = False,
    skip_deps_check: bool = False,
) -> bool:
    log = logging.getLogger("xb")
    
    start_time = time.time()
    
    if clean:
        log.info(f"Cleaning build directory: {build_dir}")
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir)
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir)
        return True
    
    # Check system dependencies
    if not skip_deps_check:
        ok = check_all_deps()
        if not ok:
            return False
    
    # Resolve external dependencies
    deps = ExternalDeps(conan_build=conan_build, src_root=src_root)
    if not deps.resolve():
        log.warning("Could not resolve all external dependencies")
    
    # Ensure proto files exist (generate or copy pre-generated)
    if not skip_proto:
        proto_ok = ensure_proto_files(src_root)
        if not proto_ok:
            log.warning("Could not ensure all proto files - build may fail")
    
    # Initialize toolchain with resolved deps
    tool = Toolchain(
        cc="g++",
        cxx_std="c++20",
        build_type=build_type,
        src_root=src_root,
        build_root=build_dir,
        include_roots=deps.include_dirs,
    )
    
    # Add the source tree's include/ as a system include
    inc_root = os.path.join(src_root, "include")
    if os.path.isdir(inc_root):
        tool.include_roots.insert(0, inc_root)
    # Proto generated headers include other .pb.h relative to proto dir
    proto_inc = os.path.abspath(os.path.join(src_root, "include", "xrpl", "proto"))
    if os.path.isdir(proto_inc):
        tool.include_roots.insert(1, proto_inc)
    
    # Initialize cache
    cache = BuildCache(cache_dir)
    
    # Build dependency graph
    graph = BuildGraph(build_dir, cache_dir)
    
    # Dependency order for libxrpl modules (from CMake RippledCore.cmake)
    dep_order = [
        ("libxrpl.beast", []),
        ("libxrpl.basics", ["libxrpl.beast"]),
        ("libxrpl.json", ["libxrpl.basics"]),
        ("libxrpl.crypto", ["libxrpl.basics"]),
        ("libxrpl.hook", ["libxrpl.basics"]),
        ("libxrpl.protocol", ["libxrpl.crypto", "libxrpl.hook", "libxrpl.json"]),
        ("libxrpl.resource", ["libxrpl.protocol"]),
        ("libxrpl.server", ["libxrpl.protocol"]),
    ]
    
    dep_map = dict(dep_order)
    
    # Discover and add libxrpl modules
    modules = discover_modules(src_root, "libxrpl")
    for mod_cfg in modules:
        mod_name = mod_cfg["_name"]
        mod_dir = mod_cfg["_dir"]
        depends = dep_map.get(mod_name, [])
        
        # Check for xbp.py overrides
        xbp = load_xbp(mod_dir)
        if xbp.get("depends"):
            depends = xbp["depends"]
        
        archive = tool.archive_path(mod_name)
        graph.add_module(
            name=mod_name,
            directory=mod_dir,
            archive=archive,
            depends=depends,
            libs=xbp.get("libs", []),
            ldflags=xbp.get("ldflags", []),
        )
        
        for src in xbp["_source_files"]:
            obj = tool.obj_path(src, mod_name)
            target = graph.add_target(mod_name, src, obj)
            extra = xbp.get("cflags", []) + xbp.get("cxxflags", [])
            target.cmd = tool.compile_cmd(src, obj, extra_cflags=extra)
        
        log.info(f"Module {mod_name}: {len(xbp['_source_files'])} sources")
    
    # Add xrpld (main daemon) source files
    xrpld_dir = os.path.join(src_root, "src", "xrpld")
    if os.path.isdir(xrpld_dir):
        xrpld_sources = sorted(
            glob.glob(os.path.join(xrpld_dir, "**", "*.cpp"), recursive=True)
        )
        # Add protobuf generated sources (they live in the include tree)
        proto_sources = find_proto_sources(src_root)
        xrpld_sources.extend(proto_sources)
        xrpld_depends = [m for m, _ in dep_order]
        
        archive = tool.archive_path("xrpld")
        graph.add_module(
            name="xrpld",
            directory=xrpld_dir,
            archive=archive,
            depends=xrpld_depends,
            libs=[],
            ldflags=[],
        )
        
        for src in xrpld_sources:
            obj = tool.obj_path(src, "xrpld")
            target = graph.add_target("xrpld", src, obj)
            target.cmd = tool.compile_cmd(src, obj, extra_cflags=[])
        
        log.info(f"xrpld module: {len(xrpld_sources)} sources")
    
    # Configure executable target
    all_mod_names = [m for m, _ in dep_order] + ["xrpld"]
    graph.add_executable(
        name="xahaud",
        output=os.path.join(build_dir, "xahaud"),
        module_names=all_mod_names,
        libs=[],
        ldflags=[],
    )
    log.info(f"Build graph: {graph.total_targets} targets, {len(graph.modules)} modules")
    actual_jobs = jobs or (os.cpu_count() or 4)
    log.info(f"Build type: {build_type}, Jobs: {actual_jobs}")
    log.info(f"Include paths: {len(tool.include_roots)} dirs, "
             f"Lib paths: {len(deps.lib_dirs)} dirs")
    
    # Run pre-scan (fingerprint all targets) - SKIP on cold builds
    cold = _is_cold_build(build_dir)
    if cold:
        log.info("Cold build detected - skipping fingerprint pre-scan")
    else:
        from xb.fingerprint import pre_scan_all, _ensure_cache, _flush_cache
        _ensure_cache(cache_dir)
        log.debug("Starting fingerprint pre-scan...")
        all_targets = graph.ordered_targets()
        pre_scan_all([t.src for t in all_targets], tool.include_roots, cache_dir, parallel=True)
        log.debug("Pre-scan complete")
    
    # Dispatch compilation
    log.info(f"Dispatching {graph.total_targets} targets to {actual_jobs} workers (mode={'cold' if cold else 'warm'})")
    result = dispatch(graph, tool.include_roots, jobs=jobs, all_have_fp=not cold)
    
    if result["failed"] > 0:
        log.error("Compilation had failures - see above for details")
        return False
    
    compiled = result["compiled"]
    
    # Skip archive+link on warm builds when nothing compiled
    if not cold and compiled == 0:
        log.info("Nothing compiled - skipping archive and link")
    else:
        # Archive modules
        log.info("Archiving modules...")
        for mod in graph.modules.values():
            if not archive_module(mod):
                log.error(f"Failed to archive module {mod.name}")
                return False
        
        # Link final binary
        log.info("Linking xahaud binary...")
        link_ok = link_executable(
            graph,
            cc=tool.cc,
            raw_cc=tool._real_cc,
            cxxflags=tool.cxxflags,
            ldflags=tool.ldflags,
            deps=deps,
        )
        if not link_ok:
            elapsed = time.time() - start_time
            log.error(f"Build failed after {elapsed:.0f}s")
            return False
    
    # Build successful
    elapsed = time.time() - start_time
    log.info(f"Build successful in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    return True


def run_clean(build_dir: str, cache_dir: str):
    """Clean build directory."""
    log = logging.getLogger("xb")
    log.info(f"Cleaning {build_dir} and {cache_dir}")
    for d in [build_dir, cache_dir]:
        if os.path.isdir(d):
            shutil.rmtree(d)
            log.info(f"Removed {d}")


def run_info(src_root: str, build_dir: str, cache_dir: str):
    """Show build information."""
    log = logging.getLogger("xb")
    log.info(f"Source root: {os.path.abspath(src_root)}")
    log.info(f"Build dir: {os.path.abspath(build_dir)}")
    log.info(f"Cache dir: {os.path.abspath(cache_dir)}")
    
    total = 0
    for subdir in ["libxrpl", "xrpld"]:
        sdir = os.path.join(src_root, "src", subdir)
        if os.path.isdir(sdir):
            count = len(glob.glob(os.path.join(sdir, "**", "*.cpp"), recursive=True))
            log.info(f"  {subdir}: {count} sources")
            total += count
    
    if os.path.isdir(cache_dir):
        cache_size = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, dn, fn in os.walk(cache_dir)
            for f in fn
        )
        log.info(f"Cache size: {cache_size / 1024 / 1024:.1f} MB")
    
    binary = os.path.join(build_dir, "xahaud")
    if os.path.isfile(binary):
        size = os.path.getsize(binary)
        mtime = os.path.getmtime(binary)
        log.info(f"Existing binary: {size / 1024 / 1024:.1f} MB, built at {time.strftime('%H:%M:%S', time.localtime(mtime))}")


def run_deps_check():
    """Check system dependencies."""
    from .sysdeps import check_all_deps, print_deps_status
    ok = check_all_deps()
    return ok


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Xahau Build System - fast, content-addressed C++ builder"
    )
    
    parser.add_argument("command", choices=["build", "info", "clean", "deps"],
                       help="Command to run")
    parser.add_argument("--jobs", type=int, default=None,
                       help="Number of parallel jobs (default: NPROC)")
    parser.add_argument("--type", choices=["Release", "Debug", "RelWithDebInfo"],
                       default="Release", help="Build type")
    parser.add_argument("--src-root", default=".",
                       help="Path to xahaud source root")
    parser.add_argument("--build-dir", default="xb-build",
                       help="Build output directory")
    parser.add_argument("--cache-dir", default="xb-cache",
                       help="Build cache directory")
    parser.add_argument("--clean", action="store_true",
                       help="Clean build directory before building")
    parser.add_argument("--conan-build", default=None,
                       help="Path to conan build directory")
    parser.add_argument("--skip-proto", action="store_true",
                       help="Skip protobuf code generation")
    parser.add_argument("--verbose", action="store_true",
                       help="Enable verbose logging")
    parser.add_argument("--skip-deps-check", action="store_true",
                       help="Skip system dependency check")
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    if args.command == "build":
        ok = build_project(
            src_root=args.src_root,
            build_dir=args.build_dir,
            cache_dir=args.cache_dir,
            build_type=args.type,
            jobs=args.jobs,
            clean=args.clean,
            conan_build=args.conan_build,
            skip_proto=args.skip_proto,
            skip_deps_check=args.skip_deps_check,
        )
        sys.exit(0 if ok else 1)
    elif args.command == "info":
        run_info(args.src_root, args.build_dir, args.cache_dir)
    elif args.command == "clean":
        run_clean(args.build_dir, args.cache_dir)
    elif args.command == "deps":
        ok = run_deps_check()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
