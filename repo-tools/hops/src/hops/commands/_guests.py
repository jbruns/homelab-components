"""What `hops host` and `hops strays` share for a Proxmox VE Host that runs no Stacks: its Guests."""

from hops import config, guests, output, proxmox
from hops.errors import UsageError

RECORD_HELP = (
    "for a Proxmox VE Host: the directory holding the record of its Guests (guests.yaml, network.yaml), relative "
    "to the working directory; default: host/<Host name>/ in the repo"
)


def add_options(parser) -> None:
    parser.add_argument("--record", metavar="DIR", default=None, help=RECORD_HELP)
    # Stacks by default, but `--host` may name a Host that runs none (e.g. a Proxmox VE Host).
    parser.set_defaults(any_named_host=True)


def on_guests_host(args, ctx, what: str) -> bool:
    """Whether this run reports Guests: the Host runs no Stacks. Refuses --record on a Host that runs Stacks, and a
    Host without the proxmox capability."""
    if ctx.host.runs_stacks:
        if args.record is not None:
            raise UsageError("--record applies only to a Host that runs no Stacks and has the proxmox capability")
        return False
    ctx.require("proxmox", what=f"{what} on a Host that runs no Stacks")
    return True


def scrub(report: dict, ctx) -> dict:
    """The report with the token secret removed from every string: read straight from the settings, as
    `hops proxmox call` does, so nothing the API or a connection error echoes can print it."""
    secrets = {config.host_setting(ctx.repo_root, ctx.host, "PROXMOX_TOKEN_SECRET", ctx.env)}
    if "proxmox" in vars(ctx):
        secrets.add(ctx.proxmox.cfg.token_secret)
    return proxmox.scrub_strings(report, sorted((s for s in secrets if s), key=len, reverse=True))


def record(args, ctx) -> guests.Record:
    directory = guests.record_dir(ctx.repo_root, ctx.host.name, args.record, ctx.cwd)
    return guests.load_record(directory, ctx.repo_root)


def show_guests(rows: list[dict], out, *, full: bool) -> None:
    headers = ["ID", "NAME", "TYPE", "KIND", "STATE", "NODE"]
    if full:
        headers += ["BACKUP AGE", "DUAL-HOMED"]
    table = []
    for g in rows:
        row = [g["id"], g["name"], g["type"], g["kind"], g["state"], g["node"]]
        if full:
            row += [age(g["backup"]), dual(g)]
        table.append(row)
    output.table(headers, table, out)


def age(backup: dict | None) -> str:
    return "none" if backup is None else f"{backup['age_days']:g}d"


def dual(g: dict) -> str | None:
    if g["dual_homed"] is None:
        return None if g["exclusive_networks"] is None else "unknown"
    if not g["dual_homed"]:
        return "no"
    return "yes (allowed)" if g["dual_homing_allowed"] else "yes"


def show_notes(notes: list[str], out) -> None:
    for note in notes:
        out.write(f"\nnote: {note}\n")
