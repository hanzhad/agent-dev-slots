# shellcheck shell=bash disable=SC2034,SC2154

DEFAULT_COMPOSE_DIR=.devcontainer
COMPOSE_DIR=${COMPOSE_DIR:-$DEFAULT_COMPOSE_DIR}

die() {
  echo "$*" >&2
  exit 1
}

usage_text() {
  cat <<USAGE
Usage: $CALLER acquire --for <holder> [--as '<name> [<ref>]'] [--allow-host <host>[,<host>]]... [--wait [--timeout <duration>]]
       $CALLER release <slot> --for <holder> — <holder> must match the lease's own holder; 'evict' takes a slot that isn't yours; wipes the slot (containers, volumes, directory) once everything in it is delivered
       $CALLER park <slot>            — give back the lease on a slot that is NOT reuse-clean, without pretending it's free
       $CALLER park <slot> --clear    — remove that mark; does not touch the slot itself
       $CALLER list [--brief] [--fast]
       $CALLER project <dir> — print the keys of <dir>/project.env, the file that connects a project
       $CALLER adopt [<dir>] [--template | --no-template] — connect a project checkout: write project.env and the .devcontainer/README.md and AGENTS.md; reports a credential file that is not ready
       $CALLER migrate-names [--dry-run] [<dir>] — rename the old AGENT_* credential names (and the modes container-env, github-app) to the FORGE_* ones in the project.env of <dir> and in the credential file it names; prints the names it changes, never a value; --dry-run writes nothing; refuses a setting given under both names
       $CALLER migrate-identity [--dry-run] — move this project's tokens, signing key and rescue directory from the names of its label to the names of its key; refuses while a lease for the project exists and never overwrites; --dry-run prints the moves and changes nothing
       $CALLER reap
       $CALLER reap --all — reap every project that holds a lease, each from its own root
       $CALLER clean [<slot>] [--all] — fast clean abandoned slots to local archives without network
       $CALLER resume <branch or lease> — restore work from an archive, report a live slot, or take a slot from remote
       $CALLER unlock — read a read-only forge token from standard input into the engine's tmpfs
       $CALLER clean [fast] — run fast clean on demand (reaps inactive leases across all projects)
       $CALLER status [--json] — report status of engine, live slots, cleaners, and archives
       $CALLER check [slow] — run the slow archive check when unlocked; reports locked and skips when no token is held
       $CALLER verdict <archive or project/lease> — evaluate unproven work in an archive and write verdict.md
       $CALLER schedule run [--once] — run the jobs in \\$STATE/schedule/jobs when they are due; --once goes through them once and exits
       $CALLER evict <slot> [--yes] [--force] | evict --stale [<days>, default 7]
       $CALLER up [name]     — acquire --for manual, same as above (no --as: a person at a terminal has no session address)
       $CALLER up <slot> [--secrets --for <holder>] — recompose that slot's stack; --secrets adds docker-compose.secrets.yml, plain 'up <slot>' is the way back. --secrets is for the session's main loop only; this script cannot tell a main loop from a subagent and does not enforce it. It needs --for naming the slot's lease holder, and never reads the overlay's env file
       $CALLER down <name> [--force]
       $CALLER restart <name> — bring a slot's containers back; clones/removes nothing
       $CALLER run <slot> -- <cmd> [args...] — exec <cmd> inside a leased, running slot's container as node
       $CALLER put <slot> <src> <dst> | $CALLER put <slot> --stdin <dst> — copy a file or directory into a leased slot's workspace
       $CALLER install <slot> — run the project's install script (install.sh in its compose directory) for a leased slot, then build its image labelled with the commit; refuses a checkout that is dirty or not on the default branch
       $CALLER doctor [--live] — check Docker, the state directory, the slot ports, the project file, its compose file and the credential file (--live also asks GitHub whether the token is valid), print the memory ceiling in force and the commit of the running image; exits non-zero naming each failed prerequisite
       $CALLER db-target <slot> — print that slot's compose project (scalar) and files (bash array), %q-quoted, for 'eval' under bash by a HOST caller that has to drive one of the slot's compose services directly

park gives back the lease on a slot that isn't reuse-clean and that the
holder has no more use for. Unlike a forced release, park
never hands the slot back as if it were free — the lease is dropped, but a
marker (who parked it, when, and what the dirty check said) takes its place,
and 'acquire' refuses a parked slot exactly as it refuses a leased one. 'list'
shows it as its own state, 'parked', not 'free'. 'park --clear' is the only
way out — it removes the marker and nothing else, so whoever clears it still
has to deal with whatever made the slot dirty in the first place. Parking a
slot that IS reuse-clean just releases it, and says so — that's what a plain
'release' would already have done.

--as is optional and free text — the owning session's address as ListAgents
prints it and SendMessage accepts it, e.g. 'fix the build [a1b2c3]'. Omitted, a lease's owner reads "-": 'up' takes it this way always,
since the person typing it has no session address to give. release leaves a
lease's holder/owner untouched; a later acquire overwrites both on takeover.
A lease has a holder and no term: there is nothing to renew. It is live while
its slot is active — a 'run' within the idle limit, or container CPU above a
threshold within it. The idle limit is one value for the whole machine
(idle_limit, in seconds, in $STATE/limits.json; default 2 hours); projects and
slots do not set it. Only live leases count against machine_max.

A slot is a lease now — 'acquire' is the only way to get one, and
it is the only thing that ever writes the ssh key that opens it. <slot> is
the directory name under ${slots_dir:-the dev-containers directory beside the project checkout}, e.g. slot-1.
acquire always builds a new slot, in the first free index under the machine
cap; a directory left behind is skipped, never reused, and a slot cannot be
named.
--wait waits for a slot instead of exiting 2 when none is free — as long as
it takes, or until --timeout, which exits 3. Exit 4: the session is at its
slot limit (keyed by CLAUDE_CODE_HOST_SESSION_ID when set, else the --as ref; or
$STATE/limits.json needs --as) — --wait does not help.

'list' without --brief answers "is this slot safe to take back", which costs a
git status, a stash list, a delivery check per branch and a worktree list per
slot. --brief drops that column when the question is only which slots exist.

'list's ACQUIRABLE column answers the OTHER question, "can I take
THIS slot right now, and if not, why not" — acquirable, leased:<holder>,
idle-unreleased:<holder> (a lease idle past the limit that nobody released
still blocks, its own word, never "free"), parked, portclash:<who>,
dirty:<what blocks reuse>, or unreadable:<n> (worktrees this process cannot
even stat). One function computes it, slot_acquire_verdict, never a second
opinion arrived at 'list's own way, so the two cannot disagree. acquire
does not take a listed slot (it builds a new one), so the column says what is
in the directory, not what acquire will do. --fast answers it from local git only, no
'gh', no docs/repo branch walk — cheaper and less certain, which the column
header says outright; DIRTY is skipped the same run, since the branch
delivery work behind it is what --fast exists to avoid, and it answers "what
would I lose" (release/evict's question), never "can I take it".

'run' is mechanics on a branch that already exists — rebase, amend,
cherry-pick, a lockfile regen, 'make lint'/'make test' before a push — never a
code change, which stays a subagent's job. It refuses, naming the
reason, on a slot that is not this project's own, not leased, parked, or
whose containers are not running ('restart <slot>' brings those back); it
never starts a container itself. Runs on the host only, same as every other
verb here that talks to Docker.

'run <slot> --live NAME [--live NAME]... -- <cmd>' runs <cmd> in the project's
one-shot live service (LIVE_SERVICE in project.env) with the named keys and
nothing else. Each value is read from LIVE_ENV_FILE in the owner's checkout
and reaches docker by name; a key that is absent or blank there refuses the
whole run. When LIVE_GATEWAY is set in project.env, the gateway service is
started before the command and removed after it (one live run at a time per
project; a leftover gateway is swept by the next run and by release). A live
run holds no forge token, and an ordinary run holds none of these keys.

'db-target' exists because a host-side task may have to drive one of
a slot's compose services with 'docker compose' from the HOST — the slot's own
container has no docker socket, so 'run' cannot do this. Same
validation as 'run' (this project's own slot, leased, running), but instead
of exec-ing anything it prints DB_RESTORE_PROJECT, a %q-quoted scalar, and
DB_RESTORE_COMPOSE_ARGS, a %q-quoted bash ARRAY literal (one element per
compose file, never one joined-and-quoted string) — meant for a caller's own
'eval "\$(...)"' under bash, whose "\${DB_RESTORE_COMPOSE_ARGS[@]}" is the
only expansion that reconstructs one argv entry per element regardless of
what a path contains; anything that joins the array first or re-splits it in
sh cuts a slot root with a space in it into two '-f' arguments. Never
re-derive it by hand: the compose file list always comes from
compose_args_of, overlay files included.
USAGE
}

usage() {
  usage_text >&2
  exit 1
}

verb_help() {
  local verb="$1" out
  out=$(usage_text | grep -F -- "$CALLER $verb")
  [[ -n "$out" ]] || { usage; return; }
  printf '%s\n' "$out"
}

valid_name() {
  [[ "$1" =~ ^[a-zA-Z0-9_-]+$ ]] || die "slot name must be alnum/-/_ only: $1"
  [[ "$1" == -* ]] && die "'$1' looks like a flag, not a slot name"
  return 0
}

parse_duration() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)([smhd]?)$ ]]; then
    local num=${BASH_REMATCH[1]} unit=${BASH_REMATCH[2]:-s}
    case "$unit" in
      s) echo "$num" ;;
      m) echo $((num * 60)) ;;
      h) echo $((num * 3600)) ;;
      d) echo $((num * 86400)) ;;
    esac
  else
    die "bad duration '$s' — a number with an optional s/m/h/d suffix, e.g. 4h"
  fi
}

reject_duration_as_holder() {
  local value="$1"
  [[ "$value" =~ ^[0-9]+[smhd]$ ]] && die "--for '$value' looks like a duration, not a holder — a lease has no term; --for names who holds the slot."
  [[ "$value" == *[[:space:]]* ]] && die "--for '$value' has whitespace in it — a holder is one token; check for an unquoted variable upstream."
  return 0
}

now_epoch() { date +%s; }

adopt_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    openssl dgst -sha256 | awk '{print $NF}'
  fi
}
