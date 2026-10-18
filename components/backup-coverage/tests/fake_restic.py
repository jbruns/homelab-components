"""Stand-in restic CLI for unit tests: `python fake_restic.py ANSWERS.json <restic args>`.

ANSWERS.json holds {"snapshots": [...], "ls": [...], "fail": false}; each call's restic args are appended to
ANSWERS.json.calls as one JSON line.
"""

import json
import sys
from pathlib import Path

answers_path, *args = sys.argv[1:]
answers = json.loads(Path(answers_path).read_text())
with open(f"{answers_path}.calls", "a") as calls:
    calls.write(json.dumps(args) + "\n")
if answers.get("fail"):
    print("Fatal: unable to open repository", file=sys.stderr)
    sys.exit(1)
subcommand = next(a for a in args if not a.startswith("-") and a not in (answers["repository"],))
if subcommand == "snapshots":
    print(json.dumps(answers["snapshots"]))
elif subcommand == "ls":
    for node in answers["ls"]:
        print(json.dumps(node))
