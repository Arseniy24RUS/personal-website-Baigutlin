#!/usr/bin/env python3
"""Compatibility entrypoint for the incremental media pipeline."""
import sys
from harvest_media_mentions import main

if __name__ == "__main__":
    sys.argv[1:1] = ['--seeds-only']
    raise SystemExit(main())
