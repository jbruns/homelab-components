"""A stand-in for the `docker` CLI, for unit tests that need no Docker Engine.

It answers only the commands startup-order.sh runs (info, ps, inspect, start, stop) from a JSON state file
($FAKE_DOCKER_STATE) and appends each start/stop to a call log ($FAKE_DOCKER_CALLS). FAKE_DOCKER_SLOW=<command>=<s>
makes that command take s seconds after it has acted; FAKE_DOCKER_DOWN_AFTER=<command> takes the daemon down once
that command has acted. The Contract Tests run the same behaviours against a real engine.

State: {"up": bool, "containers": [{"name", "labels", "running", "healthcheck", "healthy_on_start", "start_fails",
"crash_loops", "late"}]}. A crash-looping container is left `restarting` by start: listed by plain `ps`, not
running. A late container starts just after the first `ps` that lists running containers, as if deployed meanwhile.
"""

import json
import os
import re
import sys
import time


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
        only_running = "status=running" in rest
        for c in state["containers"]:
            if "-a" in rest or c.get("running") or (c.get("restarting") and not only_running):
                print(f"{c['name']}\t{c.get('labels', {}).get(label[1], '')}" if label else c["name"])
        late = [] if "-a" in rest else [c for c in state["containers"] if c.pop("late", False)]
        for c in late:
            c["running"] = True
        if late:
            with open(state_path, "w") as f:
                json.dump(state, f)
    elif command == "inspect":
        c = containers.get(rest[-1])
        if c is None:
            print(f"Error: No such object: {rest[-1]}", file=sys.stderr)
            return 1
        status = "running" if c.get("running") else "restarting" if c.get("restarting") else "exited"
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
            if command == "start" and c.get("start_fails"):
                print(f"Error response from daemon: failed to start {name}", file=sys.stderr)
                rc = 1
                continue
            c["running"] = command == "start" and not c.get("crash_loops")
            c["restarting"] = command == "start" and bool(c.get("crash_loops"))
            if command == "start" and c.get("healthcheck"):
                c["health"] = "healthy" if c.get("healthy_on_start", True) else "starting"
        if os.environ.get("FAKE_DOCKER_DOWN_AFTER") == command:
            state["up"] = False
        with open(state_path, "w") as f:
            json.dump(state, f)
        slow, _, seconds = os.environ.get("FAKE_DOCKER_SLOW", "").partition("=")
        if slow == command:
            time.sleep(float(seconds))
    else:
        print(f"fake docker: unsupported command {command!r}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
