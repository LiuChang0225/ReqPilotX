#!/usr/bin/env python3
"""Compatibility entry for the installed ReqPilotX engine capture tool."""
from reqpilotx.tools.fast_source_capture import main

if __name__ == "__main__":
    raise SystemExit(main())
