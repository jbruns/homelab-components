"""The shared front end of `hops <api> call` and `hops <api> batch`: read-only passthroughs to a Host API.

The front end owns argument parsing, the refusal checks' place in the flow, the output schema and exit codes. A
backend (one per API, e.g. hops/commands/truenas.py) owns the transport, authentication, the allowlist and
redaction, behind `Backend`:

- `check(method, params)` raises UsageError when the call is refused (off the allowlist, a refused option). It
  never touches the Host, so a refused call is never sent.
- `open()` connects and authenticates, once per command; a HopsError means the login or connection failed.
  A stateless API (e.g. a token in every request) may only set up its session here.
- `call(method, params)` sends one call and returns its raw result. It raises CallError when the API answered
  with an error and the connection is still usable; any other HopsError means the connection is lost.
- `redact(result, method)` masks credentials in one call's result before it is printed.
- `redact_params(params)` masks credentials in a call's params (e.g. a password field, or a password filter's
  value), since `batch` echoes each call's params, refused or not; the backend sends the params unmasked.

`call` prints one redacted result. `batch` reads `[{"method": ..., "params": [...]}, ...]` from stdin, opens one
connection with one login, and runs each call in order: a refused call or a CallError becomes that call's
`error`, and the batch goes on. It prints `[{"method", "params", "result"} | {"method", "params", "error"}, ...]`
and exits 0 when every call ran. A failed login or a lost connection ends the batch: it prints the entries so
far, then the failed call's entry, and exits non-zero.

Both commands target only Hosts with the backend's `capability` (see `register`).
"""

import json
from collections.abc import Callable
from typing import Any, Protocol

from hops import output
from hops.errors import CallError, HopsError, UsageError

CALL_KEYS = {"method", "params"}


class Backend(Protocol):
    api: str
    """The command group, e.g. `truenas` for `hops truenas call`."""
    capability: str
    """The hosts.yaml capability a Host needs for this API."""

    def check(self, method: str, params: list) -> None: ...
    def open(self) -> None: ...
    def call(self, method: str, params: list) -> Any: ...
    def redact(self, result: Any, method: str) -> Any: ...
    def redact_params(self, params: list) -> list: ...


def call(backend: Backend, method: str, params: list) -> Any:
    """One call's redacted result. UsageError, before connecting, when the backend refuses it."""
    backend.check(method, params)
    backend.open()
    return backend.redact(backend.call(method, params), method)


def batch(backend: Backend, calls: list[tuple[str, list]]) -> tuple[list[dict], HopsError | None]:
    """Each call's redacted result or error, in order, over one connection opened before the first call sent,
    and the failure that ended the batch early (None when every call ran). A failed login or lost connection
    stops the batch after an entry for the call it failed; the connection is not opened again. Every entry
    echoes its call's params only through `backend.redact_params`."""
    results: list[dict] = []
    opened = False
    for method, params in calls:
        entry = {"method": method, "params": backend.redact_params(params)}
        try:
            backend.check(method, params)
        except UsageError as exc:
            results.append({**entry, "error": str(exc)})
            continue
        if not opened:
            opened = True
            try:
                backend.open()
            except HopsError as exc:  # any login failure, even an API error (e.g. EACCES), ends the batch
                results.append({**entry, "error": str(exc)})
                return results, exc
        try:
            result = backend.call(method, params)
        except CallError as exc:
            results.append({**entry, "error": str(exc)})
            continue
        except HopsError as exc:
            results.append({**entry, "error": str(exc)})
            return results, exc
        results.append({**entry, "result": backend.redact(result, method)})
    return results, None


def parse_calls(text: str) -> list[tuple[str, list]]:
    """A batch's calls from its JSON input; UsageError naming the first problem."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"batch input is not JSON ({exc.msg})") from None
    if not isinstance(data, list):
        raise UsageError('batch input must be a JSON list of {"method": ..., "params": [...]} objects')
    calls = []
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise UsageError(f'call {n}: must be an object {{"method": ..., "params": [...]}}')
        if unknown := sorted(set(item) - CALL_KEYS):
            raise UsageError(f"call {n}: unknown key {', '.join(unknown)}; expected method and params")
        method, params = item.get("method"), item.get("params", [])
        if not isinstance(method, str) or not method:
            raise UsageError(f"call {n}: method must be a non-empty string")
        if not isinstance(params, list):
            raise UsageError(f"call {n}: params must be a list, one JSON value per positional parameter")
        calls.append((method, params))
    return calls


def parse_params(texts: list[str]) -> list:
    """`call`'s positional parameters, one JSON value each."""
    params = []
    for n, text in enumerate(texts, 1):
        try:
            params.append(json.loads(text))
        except json.JSONDecodeError as exc:  # not echoed: a parameter may hold a secret
            raise UsageError(f"parameter {n} is not JSON ({exc.msg})") from None
    return params


def register(
    sub,
    backend: Callable[[Any], Backend],
    *,
    call_description: str,
    method_help: str,
    param_help: str,
) -> None:
    """Add `call` and `batch` under an API's subcommand parser `sub`. `backend` is the backend class: its `api`
    and `capability` are read here, and `backend(ctx)` builds one, which must not touch the Host until
    `open()`. Both commands need no Stacks and pick only among Hosts with `capability`: the only such Host
    needs no --host, and --host naming a Host without it is refused (exit 2)."""
    api, capability = backend.api, backend.capability

    def run_call(args, ctx) -> int:
        output.print_json(call(backend(ctx), args.method, parse_params(args.params)), ctx.stdout)
        return 0

    def run_batch(args, ctx) -> int:
        results, failure = batch(backend(ctx), parse_calls(ctx.stdin.read()))
        output.print_json(results, ctx.stdout)
        if failure is not None:
            raise failure
        return 0

    p = sub.add_parser(
        "call",
        help="call a read method and print its result, with credentials redacted",
        description=call_description,
    )
    p.add_argument("method", help=method_help)
    p.add_argument("params", nargs="*", metavar="json-param", help=param_help)
    output.add_json_flag(p)
    p.set_defaults(func=run_call, stacks=False, capability=capability)

    p = sub.add_parser(
        "batch",
        help="run a JSON list of read calls from stdin over one connection and login",
        description=f'Read [{{"method": ..., "params": [...]}}, ...] from stdin and run each call as `hops '
        f"{api} call` would, in order, over one connection with one login. A refused call or a method error "
        "becomes that call's error and the batch goes on. Prints a JSON list of {method, params, result} or "
        "{method, params, error}; exits 0 when every call ran. A failed login or lost connection ends the batch "
        "after the failed call's entry, with a non-zero exit.",
    )
    output.add_json_flag(p)
    p.set_defaults(func=run_batch, stacks=False, capability=capability)
