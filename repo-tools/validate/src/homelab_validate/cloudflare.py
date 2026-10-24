"""The Cloudflare edge ranges Traefik trusts for X-Forwarded-For (check `cloudflare`).

By convention the ranges appear three times in the Host's Traefik Compose file (both entrypoints'
forwardedHeaders.trustedIPs and the CrowdSec bouncer's forwardedHeadersTrustedIPs); they must all be
valid CIDRs and identical. A consuming repo keeps its own updater that rewrites them.
"""

import ipaddress
import re

LIST_RE = re.compile(r"(forwardedHeaders\.trustedIPs=|forwardedHeadersTrustedIPs: )(\S+)")
EXPECTED_LISTS = 3


def extract(text: str) -> list[list[str]]:
    return [m.group(2).split(",") for m in LIST_RE.finditer(text)]


def problems(text: str) -> list[str]:
    lists = extract(text)
    if len(lists) != EXPECTED_LISTS:
        return [f"expected {EXPECTED_LISTS} Cloudflare range lists, found {len(lists)}"]
    out = []
    for cidr in lists[0]:
        try:
            ipaddress.ip_network(cidr)
        except ValueError:
            out.append(f"invalid CIDR {cidr!r}")
    if any(set(lst) != set(lists[0]) for lst in lists[1:]):
        out.append("the Cloudflare range lists differ; keep all three identical")
    return out
