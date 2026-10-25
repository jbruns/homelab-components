# Prose and output never name repositories, people, logins or hostnames

This repo is public and its text ends up in consumer repos' CI logs and terminals, so we decided that prose, code
comments, docstrings, CLI help and CLI output never name a repository, a person or GitHub handle, a login, or a
hostname or FQDN; they use roles instead: "the Host", "the Operator", "a consumer repo", "this repo", "the Repo
Tooling repo". Identifiers that something depends on to work are exempt: file paths, config keys, package and
image names, `uses:` refs in workflows, Renovate config, URLs that must resolve, and the license's copyright line.
Issue references are bare `#N`. Git history is not rewritten: names already in past commits stay there, since
rewriting would break every consumer's pin to a commit of this repo.

## Considered Options

- **Rewrite history to remove names from past commits.** Rejected: consumers pin reusable workflows, presets and
  tools by commit SHA, and a rewrite would orphan every pin for little gain on a repo whose history is already
  public.
