"""Entry point: python -m mcp_toolfind audit ..."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
