"""Stand-in restic CLI for unit tests: `python fake_restic.py ANSWERS.json <restic args>`.

ANSWERS.json holds {"repository": ..., "snapshots": [...], "ls": [...], "fail": false, "hang": false}; each call's
restic args are appended to ANSWERS.json.calls as one JSON line. Like restic, it reads the repository from --repo
or RESTIC_REPOSITORY, and on failure prints it (as restic does in many of its errors).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

answers_path, *args = sys.argv[1:]
answers = json.loads(Path(answers_path).read_text())
with open(f"{answers_path}.calls", "a") as calls:
    calls.write(json.dumps(args) + "\n")
if "--repo" in args:
    i = args.index("--repo")
    repository, args = args[i + 1], args[:i] + args[i + 2 :]
else:
    repository = os.environ.get("RESTIC_REPOSITORY")
if repository != answers["repository"]:
    print("Fatal: Please specify repository location (-r or --repository-file)", file=sys.stderr)
    sys.exit(1)
if answers.get("hang"):
    time.sleep(60)
if answers.get("fail"):
    repository = answers["repository"]
    masked = re.sub(r":[^:/@]+@", ":***@", repository)  # restic masks only the password in the URLs it prints
    print(f"Fatal: unable to open repository at {repository} ({masked}): connection refused", file=sys.stderr)
    sys.exit(1)
subcommand = next(a for a in args if not a.startswith("-"))
if subcommand == "snapshots":
    print(json.dumps(answers["snapshots"]))
elif subcommand == "ls":
    for node in answers["ls"]:
        print(json.dumps(node))
