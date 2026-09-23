#!/usr/bin/env python3
"""
Small access test for Scopus API key.

Usage:
  export SCOPUS_API_KEY='...'
  python scripts/test_scopus_access.py 57211062810
"""
import json
import os
import sys
import subprocess
from pathlib import Path

author_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SCOPUS_AUTHOR_ID", "57211062810")
env = os.environ.copy()
env["SCOPUS_AUTHOR_ID"] = author_id
env["SCOPUS_OUT_DIR"] = env.get("SCOPUS_OUT_DIR", "data/scopus/test")
ret = subprocess.run([sys.executable, "scripts/harvest_scopus.py"], env=env, text=True, capture_output=True)
print(ret.stdout)
if ret.stderr:
    print(ret.stderr, file=sys.stderr)
sys.exit(ret.returncode)
