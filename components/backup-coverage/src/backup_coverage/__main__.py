import os
import sys

from backup_coverage.exporter import main

sys.exit(main(sys.argv[1:], os.environ))
