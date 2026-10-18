import os
import sys

from arr_queue_exporter.exporter import main

sys.exit(main(sys.argv[1:], os.environ))
