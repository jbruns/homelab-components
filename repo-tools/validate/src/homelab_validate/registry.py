"""A minimal, stdlib-only client for the registry v2 API: read manifests and blobs by digest,
anonymously, as Renovate does. validate uses it to read an image's VOLUME paths.

Works with Docker Hub (implicit `docker.io` and `library/`), GHCR, lscr.io (which hands out GHCR
tokens) and any registry that uses the standard Bearer token challenge.
"""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class RegistryError(Exception):
    """A registry request failed or answered something unusable."""


DOCKER_HUB = "registry-1.docker.io"
# What Renovate (docker:pinDigests) asks for: with the index types listed, a multi-arch tag
# answers with its index (manifest list) digest, a single-arch tag with its manifest digest.
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)
TIMEOUT = 30


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""


def urllib_http(method: str, url: str, headers: dict[str, str]) -> Response:
    """The real transport: one HTTP request; error statuses are returned, not raised."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"registry request failed: {method} {url}: {getattr(exc, 'reason', exc)}") from exc


def locate(repository: str) -> tuple[str, str]:
    """(registry host, repository path) for an image repository as written in Compose."""
    first, _, rest = repository.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = "docker.io", repository
    if host in ("docker.io", "index.docker.io"):
        host = DOCKER_HUB
        if "/" not in path:
            path = f"library/{path}"
    return host, path


def _challenge(header: str) -> dict[str, str] | None:
    """The parameters of a `Bearer realm=...,service=...,scope=...` challenge."""
    scheme, _, params = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return dict(re.findall(r'(\w+)="([^"]*)"', params))


class Registry:
    def __init__(self, http=urllib_http):
        self.http = http
        self._tokens: dict[str, str] = {}  # by registry host and repository path

    def manifest(self, repository: str, reference: str) -> dict:
        """The manifest or index a tag or digest names, parsed; `mediaType` falls back to the
        Content-Type header for manifests that omit it."""
        host, path = locate(repository)
        url = f"https://{host}/v2/{path}/manifests/{urllib.parse.quote(reference, safe=':')}"
        resp = self._get(host, path, "GET", url, {"Accept": MANIFEST_ACCEPT})
        self._check(resp, f"{repository}@{reference}", "manifest")
        doc = json.loads(resp.body)
        doc.setdefault("mediaType", resp.headers.get("content-type", "").split(";")[0])
        return doc

    def blob(self, repository: str, digest: str) -> bytes:
        """A blob by digest, checked against it (the registry may redirect to a CDN)."""
        host, path = locate(repository)
        resp = self._get(host, path, "GET", f"https://{host}/v2/{path}/blobs/{digest}", {})
        self._check(resp, f"{repository}@{digest}", "blob")
        if "sha256:" + hashlib.sha256(resp.body).hexdigest() != digest:
            raise RegistryError(f"{repository}@{digest}: blob does not match its digest")
        return resp.body

    def _get(self, host: str, path: str, method: str, url: str, headers: dict[str, str]) -> Response:
        key = f"{host}/{path}"
        if key in self._tokens:
            headers = {**headers, "Authorization": f"Bearer {self._tokens[key]}"}
        resp = self.http(method, url, headers)
        if resp.status != 401 or "Authorization" in headers:
            return resp
        params = _challenge(resp.headers.get("www-authenticate", ""))
        if not params or "realm" not in params:
            raise RegistryError(f"{host} wants credentials validate does not have (anonymous pulls only)")
        self._tokens[key] = self._token(params)
        return self._get(host, path, method, url, headers)

    def _token(self, params: dict[str, str]) -> str:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
        url = params["realm"] + ("?" + query if query else "")
        resp = self.http("GET", url, {})
        if resp.status != 200:
            raise RegistryError(f"anonymous token request failed: GET {url}: HTTP {resp.status}")
        data = json.loads(resp.body)
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RegistryError(f"anonymous token request returned no token: GET {url}")
        return token

    @staticmethod
    def _check(resp: Response, what: str, kind: str) -> None:
        if resp.status == 404:
            raise RegistryError(f"{what}: no such {kind} in the registry")
        if resp.status != 200:
            raise RegistryError(f"{what}: registry answered HTTP {resp.status} for the {kind}")
