"""A stand-in for the `docker` CLI, for unit tests that need no Docker Engine.

It answers only the commands startup-order.sh runs (info, ps, inspect, start, stop) from a JSON state file
($FAKE_DOCKER_STATE) and appends each start/stop to a call log ($FAKE_DOCKER_CALLS). The Contract Tests run the
same behaviours against a real engine.

State: {"up": bool, "containers": [{"name", "labels", "running", "healthcheck", "healthy_on_start"}]}
"""

import json
import os
import re
import sys


def main(args):
    state_path = os.environ["FAKE_DOCKER_STATE"]
    with open(state_path) as f:
        state = json.load(f)
    if not state.get("up", True):
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        return 1
    containers = {c["name"]: c for c in state["containers"]}
    command, rest = args[0], args[1:]
    rc = 0
    if command == "info":
        pass
    elif command == "ps":
        fmt = rest[rest.index("--format") + 1]
        label = re.search(r'\.Label "([^"]+)"', fmt)
        for c in state["containers"]:
            if "-a" in rest or c.get("running"):
                print(f"{c['name']}\t{c.get('labels', {}).get(label[1], '')}" if label else c["name"])
    elif command == "inspect":
        c = containers.get(rest[-1])
        if c is None:
            print(f"Error: No such object: {rest[-1]}", file=sys.stderr)
            return 1
        status = "running" if c.get("running") else "exited"
        health = c.get("health", "none") if c.get("healthcheck") else "none"
        print(f"{status} {health}")
    elif command in ("start", "stop"):
        with open(os.environ["FAKE_DOCKER_CALLS"], "a") as log:
            log.write(" ".join([command, *rest]) + "\n")
        for name in rest:
            c = containers.get(name)
            if c is None:
                print(f"Error response from daemon: No such container: {name}", file=sys.stderr)
                rc = 1
                continue
            c["running"] = command == "start"
            if command == "start" and c.get("healthcheck"):
                c["health"] = "healthy" if c.get("healthy_on_start", True) else "starting"
        with open(state_path, "w") as f:
            json.dump(state, f)
    else:
        print(f"fake docker: unsupported command {command!r}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
