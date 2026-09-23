#!/usr/bin/env python3
"""Compatibility entrypoint: use the authenticated, credential-safe collector."""
from harvest_wos_authenticated import main

if __name__ == '__main__':
    raise SystemExit(main())
