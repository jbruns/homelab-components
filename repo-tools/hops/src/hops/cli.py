"""The hops command line: parsing, the shared Context, and error reporting."""

import argparse
import os
import sys
from functools import cached_property
from pathlib import Path

from homelab_hosts import Host, Hosts

from hops import commands, config
from hops.declared import Declared
from hops.errors import HopsError, UsageError
from hops.runner import Runner
from hops.truenas import Client as TrueNASClient
from hops.truenas import load_config as load_truenas

DESCRIPTION = """\
Ops and diagnostics for a repo's doco-cd Hosts, over SSH. hops changes runtime state only; declared
state (image tags and digests) changes only through a PR that doco-cd deploys.

Host: --host <name>; else the Host whose stack root holds the working directory; else the only Host
in hosts.yaml.
Targets: stack/service; a bare Service name when unique; a bare Stack name for all its Services."""


class Context:
    """What a command needs, each built on first use: a name error never reads .env or opens SSH."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        host: str | None = None,
        runner: Runner | None = None,
        truenas: TrueNASClient | None = None,
        env=None,
        cwd: Path | None = None,
        stdout=None,
        stderr=None,
    ):
        self.env = os.environ if env is None else env
        self.host_name = host
        self.cwd = cwd
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        if repo_root is not None:
            self.repo_root = repo_root
        if runner is not None:
            self.runner = runner
        if truenas is not None:
            self.truenas = truenas

    @cached_property
    def repo_root(self) -> Path:
        return config.find_repo_root(self.cwd)

    @cached_property
    def hosts(self) -> Hosts:
        return config.load_hosts(self.repo_root)

    @cached_property
    def host(self) -> Host:
        """The Host this command targets (--host, the working directory, or the only Host)."""
        return config.pick_host(self.hosts, self.host_name, self.cwd)

    @property
    def stack_root(self) -> str:
        return self.host.stack_root

    def has(self, *capabilities: str) -> bool:
        return all(self.host.has(c) for c in capabilities)

    def require(self, *capabilities: str, what: str) -> None:
        """A UsageError, before anything touches the Host, when the Host lacks a capability `what` needs."""
        missing = [c for c in capabilities if not self.host.has(c)]
        if missing:
            raise UsageError(
                f"{what} needs a Host with the {' and '.join(missing)} capabilit"
                f"{'ies' if len(missing) > 1 else 'y'}; Host {self.host.name!r} has "
                f"{', '.join(sorted(self.host.capabilities)) or 'none'} (hosts.yaml)"
            )

    @cached_property
    def declared(self) -> Declared:
        return Declared.load(self.repo_root, self.stack_root)

    @cached_property
    def runner(self) -> Runner:
        return Runner(config.load_host(self.repo_root, self.host, self.env))

    @cached_property
    def truenas(self) -> TrueNASClient:
        """The Host's TrueNAS middleware; a ConfigError when this Host has none."""
        return TrueNASClient(load_truenas(self.repo_root, self.host, self.env))

    def close(self) -> None:
        """End connections a command opened."""
        if "truenas" in vars(self):  # built (cached_property stores it on the instance)
            self.truenas.close()


HOST_HELP = (
    "the Host to act on (a name in hosts.yaml); default: the Host whose stack root holds the working "
    "directory, else the only Host"
)


def _add_host_option(parser: argparse.ArgumentParser) -> None:
    """--host on every leaf command too, so it can follow the command (`hops status --host nas`)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in dict.fromkeys(action.choices.values()):
                _add_host_option(child)
            return
    parser.add_argument("--host", default=argparse.SUPPRESS, help=HOST_HELP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hops", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=None, help=HOST_HELP)
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for module in commands.modules():
        module.register(sub)
    for child in dict.fromkeys(sub.choices.values()):
        _add_host_option(child)
    return parser


def main(argv: list[str] | None = None, *, ctx: Context | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = ctx or Context()
    if args.host is not None:
        ctx.host_name = args.host
    try:
        return args.func(args, ctx)
    except HopsError as exc:
        ctx.stderr.write(f"hops: error: {exc}\n")
        return exc.exit_code
    except BrokenPipeError:  # e.g. `hops status | head`
        # Python flushes stdout again at exit; point it at devnull so that flush cannot fail.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        ctx.close()
