# agent-dev-slots

A pool of disposable dev containers on one machine, for people and coding agents.

## Why

Installing an npm package runs code from strangers with your rights. A package compromised upstream can read your tokens and SSH keys the moment an install script runs. So dependency code should never run on the host, and that started this project: the host has no Node at all, and everything runs in a container.

Coding agents made a second need obvious. Several agents working on one checkout overwrite each other's changes and fight over ports. Give each its own container, clone and ports, and they cannot.

## What it does

- One pool and one cap per machine; any connected project can take a slot.
- A slot is created when taken and removed when released.
- Work left in an abandoned slot goes to a local archive before the slot is deleted.
- Every slot's compose stack gets one more file from the engine, last: a memory ceiling on the slot's app service (`4g`, or `SLOT_MEMORY_LIMIT` in `project.env`) and the commit labels on each service that builds an image. It lives in the state directory; the project's own files are never written.
- `doctor` checks Docker (and the socket, inside a container), the state directory, the slot ports and the project file, says whether the `dev-container` command is on `PATH` and which engine clone it runs from, and prints the ceiling in force and the commit of the running image.
- `install <slot>` runs the project's install script and builds the image, from a clean checkout of the default branch only; the image carries `org.opencontainers.image.revision`.
  The install script runs plain `docker compose -f` without the engine's slot-volume overlay, so it must work without it: a compose file that mounts `${WORKSPACE_DIR}` declares the external `slot-workspace` volume itself (`dev-container doctor` and `adopt` warn when it does not).

## Project requirements

Projects connected to the engine need a compose file (typically under `.devcontainer/`) satisfying three requirements:

- **Compose service named `app`**: The primary container service must be named `app` and mount the checkout at `/workspace`.
- **Unprivileged `node` user (uid 1000)**: The container image must include user `node` with uid 1000. The engine runs `chown node:node` on `/workspace` and `/home/node/.ssh`.
- **Agent git identity in compose `.env`**: Agent identity variables (`GIT_USER_NAME` and `GIT_USER_EMAIL`) are read from `<compose dir>/.env` (e.g. `.devcontainer/.env`), not from the repository root `.env`.

## Forge auth

`project.env` can give a slot a forge credential: the owner's own account, a bot account, or a GitHub App. All the keys below are optional.

- `FORGE_AUTH`: the mode, `token` or `app`. Absent, a slot holds no credential and `run` is unchanged.
- `FORGE_AUTH_FILE`: a path inside the checkout (relative, no `..`), required when `FORGE_AUTH` is set. Keep it in the project's git-ignored `.devcontainer/` like `.env`; the engine reads it in place and never copies it into a slot. It is read as data, never sourced; the last line wins and an empty value counts as missing.
- `SLOT_GIT_NAME` and `SLOT_GIT_EMAIL`: the git identity of every slot, both or neither. They replace `GIT_USER_NAME` and `GIT_USER_EMAIL` from the compose `.env` and the identity derived from a GitHub App; the engine writes them into the slot's env file on every acquire and restart, sets them as the checkout's `user.name` and `user.email`, and, for a project with `FORGE_AUTH`, as the container's system git identity. Absent, the compose `.env` (or the App's bot identity) decides as before.
- `LIVE_ENV_FILE`, `LIVE_SERVICE` and `LIVE_GATEWAY`: only for `run <slot> --live NAME -- <cmd>`, which runs the command in the compose service `LIVE_SERVICE` with just the named keys from `LIVE_ENV_FILE`, bracketed by `LIVE_GATEWAY` when set (one live run at a time per project; a leftover gateway is swept by the next run and by release).

| `FORGE_AUTH` | what `FORGE_AUTH_FILE` holds | what a slot gets |
|---|---|---|
| `token` | `GH_TOKEN` | acquire and restart write it into the slot's env file on the host (mode 0600, outside the checkout); release removes it |
| `app` | `FORGE_APP_ID`, `FORGE_APP_INSTALLATION_ID`, `FORGE_APP_SLUG`, `FORGE_APP_BOT_ID`, `FORGE_APP_KEY_FILE`, `FORGE_APP_REPOS` | no token at rest: each `run` mints a one-hour installation token on the host, caches it in `tokens/<PROJECT>` under the state directory, and hands it to that one exec by name |

The slot's env file only fills the compose file's `${VAR}s`; the engine adds `GH_TOKEN` to the environment of the service that mounts `/workspace` itself (a last overlay, from the slot's env file, so the shell you ran the engine from cannot change it), so `docker exec` and `docker run` see it with `token`. With `app` the container starts with none, and each `run` hands it the minted token. `sshd` does not pass the container's environment on to an ssh session. A project's token is never read for another. A missing setting is refused by name, never by value. The image needs `gh`; `app` also needs `openssl`, `curl` and `jq` on the host.

The first names of these settings (`AGENT_AUTH`, `AGENT_AUTH_ENV_FILE`, `AGENT_APP_*`, and the modes `container-env` and `github-app`) are no longer read: one in `project.env` or in the credential file is refused by name, with `renamed to FORGE_*; run dev-container migrate-names`.

`dev-container migrate-names [--dry-run] [<dir>]` rewrites a project's old names. It works on the `project.env` of `<dir>` (the current directory by default) and the credential file that `FORGE_AUTH_FILE` names. It prints the key names it changes and never a value, keeps each file's mode, changes nothing on a second run, and refuses, before writing anything, a setting that is given under both names. `--dry-run` prints the same and writes nothing.


### What a slot is provisioned with

With `FORGE_AUTH` set, every acquire, restart and `up` ends by running the same step as root inside the slot, so a project does not hand-write it into its compose or entrypoint files:

- git's credential helper for `https://github.com` (system git config), which answers with `x-access-token` and `$GH_TOKEN` when git asks and with nothing when there is none;
- the project's signing key, one ed25519 pair per project under `signing/<PROJECT>/` in the state directory (created on first use, never replaced), installed as `~/.ssh/container_signing` for the container user, with `gpg.format ssh`, `user.signingkey` and `commit.gpgsign true` in the system git config, in both modes. For commits to show as Verified, register the public half (`signing/<PROJECT>/container_signing.pub`) once as a signing key on the account that pushes (`gh ssh-key add <file> --type signing`); a GitHub App bot cannot register keys;
- a check that `gh` is on the container user's PATH. If not, the step fails with the line to add to the image, and the slot stays leased.

It is safe to run again (`restart` does), and it never copies a credential file into the slot: the signing key is the one file that is streamed in. It does not set `user.name` or `user.email`.

The credential file is checked at every `acquire` and `restart`, and by `adopt` and `doctor`: it exists, its mode leaves nothing to group or others (`chmod 600`), it holds the keys its mode needs, and for `app` the key file can be read and lies where the engine container can see it (the projects root, the state directory or `~/.config/machine-limits`; anything else fails with "outside what the engine container can see"). `doctor --live` also asks GitHub whether a `GH_TOKEN` is valid. Each failure prints the fix and the next command. The same checks confirm that the compose file `project.env` names exists. Nothing secret is copied into a slot: not the credential file, a key beside it, `.env` or any `*.env`, `*.pem` or `*.key` of `.devcontainer/`.

`adopt` also writes `.devcontainer/README.md` (how to fill the credential file, what is checked, how to register the signing key) and `.devcontainer/AGENTS.md` (rules for an agent in a slot) from the engine's `templates/`. They are seeded once: a file that exists is the project's and `adopt` never changes it. The stamp on the first line holds the template version it was seeded from; `doctor` says when the template has moved on and prints the `diff` command, and changes nothing. Slots receive both documents. `adopt` seeds the starter stack the same way, once and only for what is missing: `docker-compose.yml`, `engine.Dockerfile`, `engine-entrypoint.sh` and, beside the credential file, `NAME.example` listing the names to fill in (no value). Each carries a template-version stamp; `doctor` says when the template has moved on and prints the `diff` command. At every `acquire`, and in `doctor`, the compose files `project.env` names must keep the contract with the engine: a service called `app` (and `LIVE_SERVICE` when set), host ports written `${NAME}` with `NAME` in `SLOT_PORTS` and never a number, and no secret written into the file (a GitHub token, a private key, or a secret-named variable given a long literal value). A failure names the file and line, prints the fix, and changes nothing: the project's files are the owner's to fix. `adopt` also writes `.devcontainer/.gitignore` (a comment and `*`), so the folder ignores itself, credential files included, in every checkout and slot it is copied to; a project that already tracks files in `.devcontainer/` is told so and its git is left as it is.

### The engine's own git and gh calls

The engine runs git and gh on the host too: fetching before a delivery check, pushing a rescue branch on `evict`, resetting a slot, syncing the docs clone, asking GitHub whether a branch was merged. With `FORGE_AUTH` set these use the project's credential (the token, or an installation token minted from the App) and no other: the owner's own git helpers and gh login are not consulted for `https://github.com`. The secret reaches git in the environment of that one call, through a credential helper that answers only for `https` and `github.com`; it is never in an argument, a file or a log, and a slot's hooks do not run in a call that carries it.

A call can never stop to ask: `GIT_TERMINAL_PROMPT=0` is set for the whole engine and any askpass program is replaced by one that fails. A fetch that cannot authenticate is a warning (the delivered/held verdicts are then only as fresh as the last fetch) and `evict` goes on to reset to the last fetched `origin/<default branch>`. With no `FORGE_AUTH` the failure carries a one-line pointer to the two settings. A purely local project, or a remote that is not on `https://github.com`, behaves as before.

## What it does not do

It limits what dependency code can reach; it does not make that code safe. Anything you put in a container, a token included, is within reach of the code running there.

## The engine in a container

The engine runs in a container of its own, so the host needs only docker and sh. `scripts/engine install --projects-root DIR` builds the image from `image/Dockerfile` and starts the container, which restarts with docker; `scripts/engine uninstall` removes the container and nothing else: state, volumes and images stay. `scripts/dev-container-thin` passes your working directory and the arguments to the engine in the container, and returns its exit code; the `dev-container` command (below) is the one to put on `PATH`, and it uses it. A stopped or missing container is refused with the command that fixes it.

### What the container can reach

- The docker socket, which is root on the machine: whoever can run the engine can start any container with any mount. This is the one privilege; nothing else is added (no `--privileged`, no capabilities, no devices, no host network).
- The projects root you install with, read-write. A checkout outside it is refused.
- The engine's state directory, read-write, and `~/.config/machine-limits`, read-only.
- Nothing else is mounted. Your home is an empty in-memory directory, so `~/.ssh` is not there.
- Only the packages listed in `image/tools.txt` are in the image.

The state directory, the projects root and machine-limits sit at the same absolute path inside the container as on the host, because compose sends bind-mount paths to the host's docker daemon, which reads them as host paths.

## Install the command (Linux, macOS, Windows/WSL)

A project carries no launcher. `dev-container` is one command on `PATH`, and it works from any directory inside a connected project: it looks for `project.env` in the current directory and its parents, then forwards the call to the engine exactly as `scripts/dev-container-thin` does (your working directory, the arguments, the exit code, the terminal). Outside a project, the verbs that act on one (`run`, `list`, `release`, `put`, ...) are refused with the way forward, `dev-container adopt <dir>`; `adopt`, `acquire`, `status`, `doctor`, `migrate-names`, `reap --all` and `--help` work anywhere. A connected project holds only `.devcontainer/` and `project.env`: `adopt` writes no launcher (one an older `adopt` wrote is left as it is), prints the one install command when the command is not on `PATH`, and, when it knows the engine clone, seeds `.devcontainer/start.sh`. That script is the project's own from then on; it installs the command from that clone if it is missing and then runs `dev-container "$@"`.

**Linux and macOS.** Run the installer from the engine clone; it needs `sh` and nothing else (no root, no bash 4, no GNU-only tools):

```
scripts/install-cli.sh                 # a link in ~/.local/bin
scripts/install-cli.sh --dir ~/bin     # or a directory of your choice
scripts/install-cli.sh --uninstall     # removes the link, and only a link it made
```

It links `dev-container` to `scripts/dev-container-global` of that clone, so updating the clone updates the command. Running it again changes nothing; run from another clone, it repoints the link and says where it pointed before. It will not overwrite a file that is not its own link, and it never edits a shell profile: when the directory is not on `PATH` it prints the line to add (`export PATH="$HOME/.local/bin:$PATH"`), and when another `dev-container` comes first on `PATH` it names it.

**Windows.** The engine and all project work live in WSL; a Windows checkout is a read-only mirror. The Windows command is a small `dev-container.cmd` that runs the Linux command inside WSL and hands it the Windows directory it was run from, which is mapped with `wslpath` when it is inside a project. From a Windows checkout of the engine, with `-Clone` naming the clone inside WSL:

```
powershell -ExecutionPolicy Bypass -File scripts\install-cli.ps1 -Clone ~/Projects/dev-container-engine
```

That runs `scripts/install-cli.sh` inside WSL and writes `dev-container.cmd` into `%USERPROFILE%\bin` (`-Dir` changes it; `-Distro` picks a distribution; `-Uninstall` removes both). It needs no administrator rights, can be run again, and prints the line that puts the directory on `PATH` rather than changing it.

`dev-container doctor` says whether the command is on `PATH` and which engine clone it runs from.

## Status

The engine is `bin/dev-container`, with its modules in `lib/` and its tests under `tests/`. `bin/` and `lib/` must stay together: the entry point finds `lib/` as a sibling of the directory its real path is in, following symlinks, and refuses to run when the modules are not there; it does not check what the files in `lib/` are, so copy the two directories together and nothing else. Beside it is the scan in `scripts/` that guards the repository.

Run the tests with `python3 -m unittest discover -s tests`, and the scan with `python3 scripts/scan.py --tree .` and `python3 scripts/scan.py --history`. Both use only the Python standard library.
