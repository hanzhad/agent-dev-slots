<!-- dev-container-engine template: README.md version: 1 -->
# @PROJECT@: dev container setup

This directory holds what dev-container-engine needs to give each agent its
own container. `adopt` wrote this file once and it is yours now: edit it
freely, `adopt` never changes it. `doctor` says when the engine's template has
moved on and how to see the difference. Nothing here holds a secret, so a slot
receives it.

## The credential file

One file, `@CREDENTIAL_FILE@`, holds what a slot needs to push as the agent
account. `project.env` names it in `FORGE_AUTH_FILE` by a path relative to the
checkout. You fill it; the engine only reads and checks it, and never writes it,
commits it or copies it into a slot. It sits in this git-ignored directory.

Create it readable by you only, then edit it:

    umask 077
    touch @CREDENTIAL_FILE@
    chmod 600 @CREDENTIAL_FILE@

What goes in depends on `FORGE_AUTH` in `project.env`.

`FORGE_AUTH=token` needs one line, a token of the agent account:

    GH_TOKEN=<the token>

`FORGE_AUTH=app` needs a GitHub App, and no token ever enters a slot:

    FORGE_APP_ID=<the App id>
    FORGE_APP_INSTALLATION_ID=<the installation id>
    FORGE_APP_SLUG=<the App slug>
    FORGE_APP_BOT_ID=<the bot user id>
    FORGE_APP_KEY_FILE=.devcontainer/app.pem
    FORGE_APP_REPOS=<repositories the token may touch, separated by spaces>

`FORGE_APP_KEY_FILE` is the App's private key, a `.pem`. Keep it beside the
credential file, in this directory. Write it relative to the checkout, or as an
absolute path.

## What is checked, and when

Every `acquire`, `restart` and `adopt`, and `doctor`, check the file:

1. it exists;
2. its mode is 600 (no access for group or others): `chmod 600 @CREDENTIAL_FILE@`;
3. it holds every key its mode needs, none empty;
4. for `app`, the key file can be read, and the engine container can see it.

The engine runs in a container that mounts only the projects root, its state
directory (`~/.dev-containers`), `~/.config/machine-limits` and the docker
socket. A key kept anywhere else, such as `~/.ssh`, exists on the host and not
for the engine, and the check says "outside what the engine container can see".
Copy the key into this directory, `chmod 600` it, and point
`FORGE_APP_KEY_FILE` at it.

`doctor` also checks that the compose file `project.env` names exists. Add
`--live` and it asks GitHub whether a `GH_TOKEN` is still valid. Each failure
prints the fix and the next command; run `doctor` again until it is clean.

## The signing key

Commits made in a slot are signed with a key the engine keeps for the project
under `~/.dev-containers/signing/@PROJECT@/`. GitHub shows those commits as
Verified once the public key, `container_signing.pub` in that directory, is
registered as a signing key on the agent account. Do it once, logged in as that
account, with a token that has the `admin:ssh_signing_key` scope:

    gh ssh-key add ~/.dev-containers/signing/@PROJECT@/container_signing.pub --type signing --title "dev-container signing @PROJECT@"

A GitHub App bot cannot register keys. With `FORGE_AUTH=app`, commits are
Verified only when GitHub creates them through its API.

## Files in this directory

- `docker-compose.yml` and the files it builds from: the slot's containers.
- `README.md`, `AGENTS.md`: these documents. Slots receive both.
- `@CREDENTIAL_FILE@`, a key file beside it, `.env` and any `*.env` or
  `*.pem`: yours. They stay on this machine and are never copied into a slot.
