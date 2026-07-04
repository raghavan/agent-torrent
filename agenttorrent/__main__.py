"""Allow ``python -m agenttorrent`` as an alias for the ``mesh`` CLI."""

import sys

from .cli import main

sys.exit(main())
