"""`python -m mtlib ...`: the same CLI as `mt`, on every OS."""
import sys

from .cli import main

sys.exit(main())
