import os
import sys

from container_health_exporter.exporter import main

sys.exit(main(sys.argv[1:], os.environ))
