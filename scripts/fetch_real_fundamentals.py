"""Deprecated entrypoint. Use the verified Screener statement ingestion path.

This intentionally no longer writes the legacy ratio CSV.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fetch_screener_snapshots import main

if __name__ == '__main__':
    sys.exit(main())
