import os
import sys

from cwa_ingest_gate.gate import main

sys.exit(main(sys.argv[1:], os.environ))
