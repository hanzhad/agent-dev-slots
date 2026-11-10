<!-- dev-container-engine template: AGENTS.md version: 1 -->
# Rules for an agent working in a @PROJECT@ slot

You are inside a slot: a container with its own clone of the repository, its
own ports and its own volumes. These rules come from dev-container-engine. The
project's own `AGENTS.md` and `CLAUDE.md` still apply, and win where they differ.

## What a slot is

- The slot is yours until it is released. Nothing else uses your clone, your
  ports or your volumes.
- Work in `/workspace`. Commit and push from there; a change that is not pushed
  is lost when the slot goes.
- Commits are signed with the project's key. Do not change the git identity or
  the signing settings.

## Credentials

- Never read, print, copy, log or commit a credential. That covers the token
  or App settings in `@CREDENTIAL_FILE@`, a private key, and every `.env`.
- A slot does not hold the credential file and never receives it. If a command
  needs a token, use the one the engine gives it by name; do not go looking for
  the file on the host.
- If a credential looks wrong or missing, say so in your report. Do not work
  around it, and do not ask for the value in a message.

## Running things

- Run commands in the slot with the project's thin command, from the checkout:
  `bin/dev-container run <slot> -- <command>`. Dependency installs and test runs
  belong here, not on the host.
- Keep to the ports the slot was given. Do not bind a port outside its block.
- Do not edit `.devcontainer/`, `project.env` or `bin/dev-container` from a slot:
  they belong to the main checkout and a change here does not reach it.

## When you finish

- Push your branch and open or update the pull request before you stop.
- Release the slot you were given as the last step: `bin/dev-container release
  <slot> --for <your holder>`. A refusal lists what is left; fix it and run the
  release again. Never force a release to get past a refusal.
- If you cannot finish, say what is left and where: branch, files, the command
  that fails. The slot stays leased while it is in use; do not abandon it.
