# This repo stays agnostic of its consumers

Several consumer repos pin this repo's Components, Repo Tooling, reusable workflows and Renovate presets, and each
keeps its own ADRs, numbered independently, so the same number means different decisions in different consumers.
We decided this repo names no consumer repo, Host, person or login, and never cites a consumer's ADRs: its prose
speaks of roles ("a consumer repo", "the Host", "the Operator"), and a rule this repo enforces is stated in full
where it is enforced. A citation that is right for one consumer would be wrong or meaningless for another, and a
name ties shared code to one deployment it must not depend on.

## Consequences

- Tool output and help (`homelab-validate`, `hops`, `homelab-declared-containers`) state the rule they enforce
  rather than an ADR number.
- Test fixtures use placeholder Hosts (`host-a`, `host-b`) and domains (`example.com`), not real ones.
- This repo's own ADRs live here and may be linked from its own docs.
