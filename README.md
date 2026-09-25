# xb Build System 

## Architecture (7 modules, ~11KB Python)
xb/
  __init__.py    - Package init
  __main__.py    - python -m xb entry
  build.py       - Orchestrator (CLI, phases, progress)
  fingerprint.py - Content-addressed build cache (SHA-256)
  project.py     - xbp.py config loader + module discovery
  target.py      - BuildGraph (topological sort, Target, Module)
  compiler.py    - Multi-threaded compilation dispatcher
  linker.py      - ar archives + final link step
  toolchain.py   - g++ flags, defines, include/lib paths
  deps.py        - External dep resolver (conan cache + bundled)

## How It Works
1. Discover modules from xbp.py configs in src/libxrpl/*/
2. Build dependency graph with topological ordering
3. Content-address compile: SHA-256 fingerprint of (source + headers + cmdline)
4. Multi-threaded dispatch with progress reporting
5. Archive each module with ar
6. Final link step for xahaud binary

## xbp.py Format (in each module dir)
config = {
    "sources": ["**/*.cpp"],
    "depends": ["libxrpl.basics"],  # list of module names
    "libs": [],                     # system libs to link
    "cflags": [], "cxxflags": [],
}

## Module Dependency Graph (from CMake)
Level 01: libxrpl.beast (no deps)
Level 02: libxrpl.basics -> beast
Level 03: libxrpl.json, libxrpl.crypto, libxrpl.hook -> basics
Level 04: libxrpl.protocol -> crypto, hook, json
Level 05: libxrpl.resource, libxrpl.server -> protocol
Top:      xrpld (main) -> all above

## Build Stats (397 sources total)
  libxrpl: 104 sources (8 modules)
  xrpld: 293 sources
  Compile: ~23 min first build on 18 threads
  Caching: content-addressed, skip unchanged objects

## Current Remaining Issues (environmental, not build-system bugs)
1. SOCI system headers need -DSOCI_HAVE_CXX11 (SOCI was built for C++98 on this system)
   Fix: install SOCI from conan or add define
2. protoc version mismatch (3.21.12 generated vs 3.21.0 on system)
   Fix: use conan's protoc or regenerate with system protoc

## Usage
  cd /root/xahaud
  python3 -m xb build --type Release --jobs 18
  python3 -m xb clean
  python3 -m xb info
