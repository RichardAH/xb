#!/usr/bin/env python3
"""
xb – Xahau Build System

Usage:
    python -m xb build [--jobs N] [--type Release|Debug] [--clean]
    ./xb build [--jobs N] [--type Release|Debug] [--clean]
"""

import sys
import os

# Add parent directory to path so we can import xb modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xb.build import main

if __name__ == "__main__":
    main()
