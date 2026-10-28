"""Runs inside the pinned CWA image for the Contract Tests: one gate run on /library with real calibredb and the
Hardcover books given on stdin, printing its outcomes as JSON."""

import json
import sys
from datetime import timedelta

from gate_stubs import Hardcover

from cwa_ingest_gate import gate
from cwa_ingest_gate.hardcover import Vocabulary

spec = json.load(sys.stdin)
state = gate.State("/state")
state.save(spec["mark"])
vocab = Vocabulary({"tags": ["Science Fiction"], "synonyms": {"Sci-Fi": "Science Fiction"}}, {})
config = gate.Config(library="/library", settle=timedelta(0))
print(json.dumps(gate.run_once(config, state, Hardcover(*spec["hardcover"]), vocab)))
