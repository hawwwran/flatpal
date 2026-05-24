import sys

from .app import main
from .debuglog import setup as setup_debug_log


if __name__ == "__main__":
    setup_debug_log()
    sys.exit(main())
