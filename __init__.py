#!/usr/bin/env python3
"""
xb – Xahau Build

A bespoke build system for xahaud, inspired by Boost.Build (b2) but designed
for maximum parallel compilation speed with content-addressed caching.

Key design decisions:
  • Content-based fingerprinting – objects are rebuilt only when source,
    headers, defines, or flags actually change (not on timestamp).
  • Dispatch-to-all-cores model – compile jobs are fan-out to NPROC workers
    via a bounded work queue with no per-file shell overhead.
  • xbp (Xahau Build Project) files – simple, declarative config in each
    source directory, Python-based for maximum flexibility.
  • Zero external build-tool dependency – pure Python 3, no CMake/Ninja/make
    required.  (ninja is used only for protobuf code-gen when gRPC is needed.)
"""
