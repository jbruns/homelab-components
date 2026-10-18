import os
import sys

from sabnzbd_status_exporter.exporter import main

sys.exit(main(sys.argv[1:], os.environ))
