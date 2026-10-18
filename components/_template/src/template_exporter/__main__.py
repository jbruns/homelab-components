import os
import sys

from template_exporter.exporter import main

sys.exit(main(sys.argv[1:], os.environ))
