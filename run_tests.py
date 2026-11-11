#!/usr/bin/env python3
"""Run the whole fixture corpus with one command:  python run_tests.py"""
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                       cwd=str(root))
    sys.exit(r.returncode)
