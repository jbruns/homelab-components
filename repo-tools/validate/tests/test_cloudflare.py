"""Tests for the Cloudflare range checker (ported from a consumer repo's tools/cloudflare-ips)."""

import pytest

from homelab_validate import cloudflare

OLD = "173.245.48.0/20,2400:cb00::/32"


def traefik(*lists: str) -> str:
    keys = [
        "--entrypoints.https.forwardedHeaders.trustedIPs=",
        "--entrypoints.http.forwardedHeaders.trustedIPs=",
        "forwardedHeadersTrustedIPs: ",
    ]
    return "".join(f"      - {k}{v}\n" for k, v in zip(keys, lists, strict=False))


def test_extract():
    assert cloudflare.extract(traefik(OLD, "1.1.1.0/24")) == [OLD.split(","), ["1.1.1.0/24"]]


def test_problems_ok():
    assert cloudflare.problems(traefik(OLD, OLD, OLD)) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        (traefik(OLD, OLD), ["expected 3 Cloudflare range lists, found 2"]),
        (traefik("1.2.3.4/33", "1.2.3.4/33", "1.2.3.4/33"), ["invalid CIDR '1.2.3.4/33'"]),
        (traefik(OLD, OLD, "173.245.48.0/20"), ["the Cloudflare range lists differ; keep all three identical"]),
    ],
)
def test_problems(text, expected):
    assert cloudflare.problems(text) == expected


def test_problems_ignores_order():
    reordered = ",".join(reversed(OLD.split(",")))
    assert cloudflare.problems(traefik(OLD, reordered, OLD)) == []
