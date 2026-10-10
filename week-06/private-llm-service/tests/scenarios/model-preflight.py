"""Bounded selected-model preflight invoked only by the fixed dispatcher."""
import asyncio
import sys
from harness.model_preflight import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
