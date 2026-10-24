"""hops recreate <target…>: doco-cd force-recreates each Service (hops never runs docker compose)."""

from hops import doco, lifecycle
from hops.errors import UsageError

HELP = "force-recreate Services through doco-cd's REST API"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "recreate",
        help=HELP,
        description=(
            HELP + ", e.g. after ContainerLeftoverName. Runtime-reversible: doco-cd recreates the Service "
            "from the Stack it last deployed. Decrypts the API key with sops on this machine and sends it "
            "on stdin to a container on doco-cd's network (not one being recreated), which calls doco-cd."
        ),
    )
    lifecycle.add_targets(p)
    p.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="give up on each Service after this long (default 600)",
    )
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    services = ctx.declared.resolve(args.targets)
    cfg = ctx.host.doco_cd
    if any(s.key == doco.service_key(cfg) for s in services):
        raise UsageError(f"{doco.service_key(cfg)} cannot recreate itself; it replaces itself on its own Deploy")
    if args.timeout <= 0:
        raise UsageError("--timeout must be a positive number of seconds")
    via = doco.transport(ctx.runner, ctx.declared, services, cfg)
    key = doco.api_key(ctx.repo_root, ctx.stack_root, cfg)
    for s in services:
        ctx.stdout.write(f"Recreating {s.key} through doco-cd (from {via})...\n")
        ctx.stdout.flush()
        doco.recreate(ctx.runner, via, s, key, args.timeout, cfg)
        ctx.stdout.write(f"Recreated {s.key}.\n")
    return 0
