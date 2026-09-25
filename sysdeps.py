"""
System dependency checker for xb.

Detects missing build tools and library headers on the host system,
prompts the user to install them via apt, and re-checks after install.

Dependencies are defined in two places:
  1. Built-in tool checks in this module (g++, protoc, ccache, etc.)
  2. Project-level library requirements passed via xbp.py or build.py

Usage:
    from xb.sysdeps import check_all_deps

    # Quick one-liner (interactive, prompts for each missing dep)
    ok = check_all_deps()

    # Project-supplied extra deps
    ok = check_all_deps(extra_deps={
        "rocksdb": {"apt_pkg": "librocksdb-dev",
                    "check": lambda: find_header("rocksdb/db.h")},
    })
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("xb.sysdeps")

# ---------------------------------------------------------------------------
# Dependency definition
# ---------------------------------------------------------------------------


@dataclass
class Dep:
    """A single system dependency to check."""

    name: str                    # human-friendly label
    apt_pkg: str                # apt package name (may be empty if not apt-installable)
    check: Callable[[], bool]   # returns True if the dep is present
    hint: str = ""              # extra text shown when missing
    optional: bool = False      # don't block build if missing
    # Custom install command(s) – called if default apt install fails
    install_alternatives: list[str] | None = None


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

_HEADER_SEARCH_PATHS = [
    "/usr/include",
    "/usr/local/include",
    "/usr/lib/gcc/x86_64-linux-gnu/*/include",
    "/usr/include/x86_64-linux-gnu",
]


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _find_header(header: str, extra_paths: list[str] | None = None) -> str | None:
    """Try to locate *header* by asking the compiler."""
    # Quick check: ask gcc where it would find the file
    try:
        result = subprocess.run(
            ["g++", "-print-file-name", header],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() != header:
            return result.stdout.strip()
    except Exception:
        pass

    # Fallback: brute-force search
    paths = _HEADER_SEARCH_PATHS + (extra_paths or [])
    for p in paths:
        candidate = os.path.join(p, header)
        if os.path.isfile(candidate):
            return candidate
    return None


def _pkg_config_ok(pkg: str) -> bool:
    """Return True if pkg-config can find *pkg*."""
    try:
        subprocess.run(
            ["pkg-config", "--exists", pkg],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ubuntu / Debian version detection
# ---------------------------------------------------------------------------

def _os_version() -> tuple[str, int, int] | None:
    """Detect OS name and major.minor version, e.g. ('ubuntu', 24, 04)."""
    try:
        with open("/etc/os-release") as f:
            name = ""
            ver_major = ver_minor = 0
            for line in f:
                if line.startswith("ID="):
                    name = line.split("=")[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    parts = line.split("=")[1].strip().strip('"').split(".")
                    ver_major = int(parts[0]) if parts else 0
                    ver_minor = int(parts[1]) if len(parts) > 1 else 0
            return (name, ver_major, ver_minor)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Built-in dependency definitions
# ---------------------------------------------------------------------------

def _builtin_deps() -> list[Dep]:
    """Return the default set of tool dependencies."""

    deps = [
        Dep(
            name="C++ compiler (g++ or clang++)",
            apt_pkg="g++",
            check=lambda: _which("g++") is not None or _which("clang++") is not None,
            hint="Also works: clang++ from package clang",
        ),
        Dep(
            name="make",
            apt_pkg="make",
            check=lambda: _which("make") is not None,
        ),
        Dep(
            name="ar (archiver)",
            apt_pkg="binutils",
            check=lambda: _which("ar") is not None,
        ),
        Dep(
            name="protoc (Protocol Buffers compiler)",
            apt_pkg="protobuf-compiler",
            check=lambda: _which("protoc") is not None,
            hint="Version >= 3.21.0 recommended for xahaud",
        ),
        Dep(
            name="pkg-config",
            apt_pkg="pkg-config",
            check=lambda: _which("pkg-config") is not None,
        ),
        Dep(
            name="git",
            apt_pkg="git",
            check=lambda: _which("git") is not None,
        ),
        Dep(
            name="ccache (optional, speeds up rebuilds)",
            apt_pkg="ccache",
            check=lambda: _which("ccache") is not None,
            optional=True,
        ),
    ]

    return deps


# ---------------------------------------------------------------------------
# Interactive install
# ---------------------------------------------------------------------------

def _run_install(cmd: str) -> bool:
    """Run a single install command, return True if it succeeded."""
    log.info(f"Running: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, timeout=600,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("Install command timed out")
        return False
    except Exception as e:
        log.error(f"Install command failed: {e}")
        return False


def _prompt_install(dep: Dep) -> bool:
    """
    Ask the user if they want to install *dep*.
    Returns True if the dep is now available.
    """
    apt_cmd = f"sudo apt-get install -y {dep.apt_pkg}" if dep.apt_pkg else None

    lines = [f"\nMissing: {dep.name}"]
    if dep.apt_pkg:
        lines.append(f"  apt package: {dep.apt_pkg}")
    if dep.hint:
        lines.append(f"  Note: {dep.hint}")

    # Build list of commands to try
    commands_to_try: list[str] = []
    if apt_cmd:
        commands_to_try.append(apt_cmd)
    if dep.install_alternatives:
        for alt in dep.install_alternatives:
            commands_to_try.append(alt)

    lines.append("")
    for i, cmd in enumerate(commands_to_try, 1):
        lines.append(f"  Step {i}: {cmd}")

    lines.append("")
    lines.append("  Install now? [y/N] ")

    try:
        answer = input("\n".join(lines)).strip().lower()
        if answer not in ("y", "yes"):
            log.info(f"Skipping install of {dep.apt_pkg or dep.name}")
            return False

        for i, cmd in enumerate(commands_to_try, 1):
            if len(commands_to_try) > 1:
                log.info(f"Trying step {i}/{len(commands_to_try)}...")
            ok = _run_install(cmd)
            if ok:
                # Re-check
                try:
                    if dep.check():
                        log.info(f"Installed {dep.name} successfully")
                        return True
                except Exception:
                    pass
            else:
                if i < len(commands_to_try):
                    log.warning(f"Step {i} failed, trying next alternative...")
                else:
                    log.error(f"Install of {dep.apt_pkg or dep.name} failed")

        return False

    except (KeyboardInterrupt, EOFError):
        log.info("Interrupted")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_all_deps(
    extra_deps: dict[str, dict] | None = None,
    auto_install: bool = True,
    quiet: bool = False,
) -> bool:
    """
    Check all system dependencies.

    Parameters
    ----------
    extra_deps : dict
        Project-supplied dependency overrides. Keys are arbitrary identifiers.
        Values are dicts with "apt_pkg" (str) and "check" (callable).
    auto_install : bool
        If True, prompt the user to install missing dependencies.
    quiet : bool
        If True, don't print progress messages.

    Returns
    -------
    bool
        True if all required (non-optional) deps are present.
    """
    deps = _builtin_deps()

    # Add project-supplied extra deps
    if extra_deps:
        for key, spec in extra_deps.items():
            dep = Dep(
                name=spec.get("name", key),
                apt_pkg=spec.get("apt_pkg", ""),
                check=spec["check"],
                hint=spec.get("hint", ""),
                optional=spec.get("optional", False),
            )
            deps.append(dep)

    if not quiet:
        log.info(f"Checking {len(deps)} system dependencies...")

    missing: list[Dep] = []
    for dep in deps:
        try:
            ok = dep.check()
        except Exception as e:
            log.warning(f"Check failed for {dep.name}: {e}")
            ok = False

        if ok:
            if not quiet:
                log.debug(f"  ✓ {dep.name}")
        else:
            missing.append(dep)
            if not quiet:
                status = "optional" if dep.optional else "REQUIRED"
                log.warning(f"  ✗ {dep.name} [{status}]")

    if not missing:
        if not quiet:
            log.info("All system dependencies satisfied")
        return True

    # Prompt for installs
    blocked = False
    for dep in missing:
        if dep.optional:
            if not quiet:
                log.info(f"  (optional) skipping {dep.name}")
            continue
        if auto_install:
            ok = _prompt_install(dep)
            if ok:
                if not quiet:
                    log.info(f"  ✓ {dep.name} (now present)")
                continue
            blocked = True
            if not quiet:
                log.error(f"  ✗ {dep.name} is still not available")
        else:
            blocked = True

    if blocked:
        if not quiet:
            log.error("Cannot proceed: missing required dependencies")
        return False

    if not quiet:
        log.info("Required dependencies satisfied (optional ones skipped)")
    return True


# ---------------------------------------------------------------------------
# Convenience: header check function
# ---------------------------------------------------------------------------

def find_header(header: str, extra_paths: list[str] | None = None) -> str | None:
    """Public wrapper around _find_header."""
    return _find_header(header, extra_paths)
