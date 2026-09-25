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
from .linker import link
from .cache import BuildCache
from .deps import ExternalDeps, generate_protobuf, find_proto_sources


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def build_project(
    src_root: str = ".",
    build_dir: str = "xb-build",
    cache_dir: str = "xb-cache",
    build_type: str = "Release",
    jobs: int | None = None,
    clean: bool = False,
    conan_build: str | None = None,
    skip_proto: bool = False,
) -> dict:
    """
    Build the complete xahaud project.

    Returns a dict with build statistics.
    """
    log = logging.getLogger("xb.build")
    src_root = os.path.abspath(src_root)
    build_dir = os.path.abspath(build_dir)
    cache_dir = os.path.abspath(cache_dir)

    if clean:
        log.info(f"Cleaning build directory: {build_dir}")
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)

    os.makedirs(build_dir, exist_ok=True)

    # Auto-detect conan build directory if not specified
    if conan_build is None:
        for candidate in ["release-build", "build/Release"]:
            cb = os.path.join(src_root, candidate)
            if os.path.isdir(cb):
                conan_build = cb
                break

    # Resolve external dependencies
    deps = ExternalDeps(conan_build=conan_build)
    deps.resolve()

    # Add bundled external include dirs from source tree
    bundled = os.path.join(src_root, "external")
    if os.path.isdir(bundled):
        for edir in sorted(os.listdir(bundled)):
            epath = os.path.join(bundled, edir)
            if os.path.isdir(epath):
                # Add to includes if it has .h files at top level
                for hf in os.listdir(epath):
                    if hf.endswith(".h"):
                        deps.include_dirs.append(epath)
                        break
                # Also add subdirectories with headers
                for sf in os.listdir(epath):
                    sp = os.path.join(epath, sf)
                    if os.path.isdir(sp):
                        for hf in os.listdir(sp):
                            if hf.endswith(".h"):
                                deps.include_dirs.append(sp)
                                break

    # Remove duplicates
    deps.include_dirs = list(dict.fromkeys(deps.include_dirs))
    deps.lib_dirs = list(dict.fromkeys(deps.lib_dirs))

    if not skip_proto:
        generate_protobuf(src_root, build_dir)

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
    proto_inc = os.path.join(src_root, "include", "xrpl", "proto")
    if os.path.isdir(proto_inc):
        tool.include_roots.append(proto_inc)

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

        # Add source targets
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
            target.cmd = tool.compile_cmd(src, obj)

        log.info(f"xrpld module: {len(xrpld_sources)} sources")

    # Add test sources in debug mode
    if build_type in ("Debug", "RelWithDebInfo"):
        test_dir = os.path.join(src_root, "src", "test")
        if os.path.isdir(test_dir):
            test_sources = sorted(
                glob.glob(os.path.join(test_dir, "**", "*.cpp"), recursive=True)
            )
            test_depends = [m for m, _ in dep_order] + ["xrpld"]

            archive = tool.archive_path("test")
            graph.add_module(
                name="test",
                directory=test_dir,
                archive=archive,
                depends=test_depends,
                libs=[],
                ldflags=[],
            )
            tool.add_define("ENABLE_TESTS")

            for src in test_sources:
                obj = tool.obj_path(src, "test")
                target = graph.add_target("test", src, obj)
                target.cmd = tool.compile_cmd(src, obj)

            log.info(f"test module: {len(test_sources)} sources")

    # Add executable
    exe_output = os.path.join(build_dir, "xahaud")
    all_mods = [m for m, _ in dep_order] + ["xrpld"]
    if build_type in ("Debug", "RelWithDebInfo"):
        all_mods.append("test")

    graph.add_executable(
        name="xahaud",
        output=exe_output,
        module_names=all_mods,
        libs=[],
        ldflags=[],
    )

    # Print summary
    log.info(f"Build graph: {graph.total_targets} targets, {len(graph.modules)} modules")
    log.info(f"Build type: {build_type}, Jobs: {jobs or 'auto'}")
    log.info(f"Include paths: {len(deps.include_dirs)} dirs, Lib paths: {len(deps.lib_dirs)} dirs")

    # Phase 0: Pre-scan dependencies for cold builds
    # Parallel g++ -MM scan for files that need it
    from xb.fingerprint import pre_scan_all, _ensure_cache, _DEP_CACHE, _flush_cache
    
    all_targets = graph.ordered_targets()
    
    # Quick check: if ALL .o files have valid .fp sidecars, skip pre-scan entirely
    all_cached = True
    for t in all_targets:
        if not os.path.isfile(t.obj + ".fp"):
            all_cached = False
            break
    
    # Phase 0: Pre-scan dependencies
    from xb.fingerprint import _ensure_cache
    _ensure_cache(cache_dir)
    pre_scan_all([t.src for t in all_targets], tool.include_roots, cache_dir, parallel=True)

    # Phase 1: Compile
    t0 = time.monotonic()

    def progress_cb(done, total, target):
        if done % 50 == 0 or done == total:
            pct = 100 * done / total
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            log.info(
                f"Compile: {done}/{total} ({pct:.0f}%) "
                f"{rate:.1f} files/s ETA {eta:.0f}s "
                f"status={target.status}"
            )

    compile_stats = dispatch(graph, tool.include_roots, jobs, progress_cb, all_cached)
    
    # Pre-scan complete
    compile_elapsed = time.monotonic() - t0
    
    # Pre-scan complete

    # Check for failures
    if compile_stats["failed"] > 0:
        log.error(f"Compilation had {compile_stats['failed']} failures")
        return {
            **compile_stats,
            "phase": "compile_failed",
            "elapsed": compile_elapsed,
        }

    # Flush fingerprint cache to disk so next build starts warm
    from xb.fingerprint import _flush_cache
    _flush_cache()

    # Phase 2: Link (skip if nothing changed and binary is up to date)
    t1 = time.monotonic()
    link_ok = False
    link_elapsed = 0
    
    # Check if we need to link
    need_link = compile_stats["compiled"] > 0 or not os.path.isfile(
        os.path.join(build_dir, "xahaud")
    )
    
    if need_link:
        log.info("Starting link phase...")
        link_ok = link(
            graph,
            cc="g++",
            cxxflags=tool.cxxflags,
            ldflags=tool.ldflags,
            libs=deps.libs,
            deps=deps,
        )
    else:
        log.info("Skipping link phase (nothing changed)")
    link_elapsed = time.monotonic() - t1

    # Final stats
    total_elapsed = time.monotonic() - t0
    cache_stats = cache.stats()

    # If link was skipped, treat it as successful
    link_ok = link_ok or not need_link
    
    result = {
        **compile_stats,
        "link_ok": link_ok,
        "phase": "complete" if link_ok else "link_failed",
        "compile_elapsed": round(compile_elapsed, 1),
        "link_elapsed": round(link_elapsed, 1),
        "total_elapsed": round(total_elapsed, 1),
        "cache": cache_stats,
        "build_dir": build_dir,
        "cache_dir": cache_dir,
    }

    log.info(f"Build {'succeeded' if link_ok else 'failed'} in {total_elapsed:.1f}s")
    log.info(
        f"  Compiled: {compile_stats['compiled']}  "
        f"Skipped: {compile_stats['skipped']}  "
        f"Failed: {compile_stats['failed']}"
    )
    log.info(f"Cache: {cache_stats['objects']} objects ({cache_stats['size_mb']:.1f} MB)")

    # Write build manifest
    manifest = {
        "build_type": build_type,
        "total_targets": graph.total_targets,
        "modules": len(graph.modules),
        "stats": result,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = os.path.join(build_dir, "xb-manifest.json")
    Path(manifest_path).write_text(json.dumps(manifest, indent=2) + "\n")

    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="xb",
        description="Xahau Build System – fast, content-addressed C++ builder",
    )
    parser.add_argument(
        "command",
        choices=["build", "info", "clean"],
        default="build",
    )
    parser.add_argument(
        "--jobs", "-j", type=int, default=None,
        help="Parallel jobs (default: NPROC)",
    )
    parser.add_argument(
        "--type", "-t", default="Release",
        choices=["Release", "Debug", "RelWithDebInfo"],
        help="Build type",
    )
    parser.add_argument(
        "--src-root", default=".",
        help="Source root directory",
    )
    parser.add_argument(
        "--build-dir", default="xb-build",
        help="Build output directory",
    )
    parser.add_argument(
        "--cache-dir", default="xb-cache",
        help="Build cache directory",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Clean build directory first",
    )
    parser.add_argument(
        "--conan-build", default=None,
        help="Conan build directory (auto-detected if not set)",
    )
    parser.add_argument(
        "--skip-proto", action="store_true",
        help="Skip protobuf code generation",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "clean":
        log = logging.getLogger("xb")
        log.info(f"Cleaning {args.build_dir} and {args.cache_dir}")
        shutil.rmtree(args.build_dir, ignore_errors=True)
        shutil.rmtree(args.cache_dir, ignore_errors=True)
        log.info("Clean complete")
        return

    if args.command == "info":
        log = logging.getLogger("xb")
        cache = BuildCache(args.cache_dir)
        stats = cache.stats()
        log.info(f"Cache stats: {json.dumps(stats, indent=2)}")
        return

    if args.command == "build":
        result = build_project(
            src_root=args.src_root,
            build_dir=args.build_dir,
            cache_dir=args.cache_dir,
            build_type=args.type,
            jobs=args.jobs,
            clean=args.clean,
            conan_build=args.conan_build,
            skip_proto=args.skip_proto,
        )
        if result.get("link_ok"):
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
