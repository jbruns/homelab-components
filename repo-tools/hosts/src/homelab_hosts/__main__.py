"""homelab-hosts: validate the hosts.yaml of the repo containing the working directory and list its Hosts."""

import argparse
import json
import sys

from homelab_hosts import HostsError, load_hosts


def main(argv=None):
    parser = argparse.ArgumentParser(prog="homelab-hosts", description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the Hosts as JSON")
    args = parser.parse_args(argv)
    try:
        hosts = load_hosts()
    except HostsError as e:
        for problem in e.problems:
            print(problem, file=sys.stderr)
        return 1
    if args.json:
        rows = [
            {
                "name": h.name,
                "stack_root": h.stack_root,
                "deployer": h.deployer,
                "capabilities": sorted(h.capabilities),
                "doco_cd": vars(h.doco_cd),
            }
            for h in hosts.values()
        ]
        print(json.dumps({"default_branch": hosts.default_branch, "hosts": rows}, indent=2))
    else:
        for h in hosts.values():
            print(f"{h.name}\t{h.stack_root}\t{h.deployer}\t{','.join(sorted(h.capabilities)) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
