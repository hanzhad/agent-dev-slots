# shellcheck shell=bash disable=SC2034,SC2154

# Per-slot extra firewall hosts: `acquire --allow-host HOST` opens HOST for that
# one slot. The list is the acquirer's, never the slot's: it is kept in the
# engine's state directory ($STATE/allow-hosts/), which no slot mounts, and it
# reaches the container only through the slot's own env file (also under
# $STATE), which is what compose reads FIREWALL_EXTRA_DOMAINS from. Nothing in
# the checkout (the in-checkout .env is ignored and removed) can change it.
#
#   <slot>.hosts  the validated hosts the acquirer asked for, one per line
#   <slot>.base   the project's own FIREWALL_EXTRA_DOMAINS as it was before the
#                 slot's hosts were added, so a re-sync never repeats them
#
# Both are removed when the lease ends, and the env value goes back to the base.

ALLOW_HOSTS_VAR=FIREWALL_EXTRA_DOMAINS
ALLOW_HOSTS_REQUESTED=()

allow_hosts_file_of() { echo "$STATE/allow-hosts/$1.hosts"; }
allow_hosts_base_file_of() { echo "$STATE/allow-hosts/$1.base"; }

# One host: a dotted DNS name (letters, digits and inner hyphens in each label).
# No scheme, path, port, wildcard, space, newline or trailing dot. Prints it in
# lower case; fails on anything else.
allow_host_normalise() {
  local host="${1,,}"
  (( ${#host} >= 3 && ${#host} <= 253 )) || return 1
  [[ "$host" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || return 1
  printf '%s\n' "$host"
}

# Adds the hosts of one --allow-host value (a comma list) to
# ALLOW_HOSTS_REQUESTED, or dies naming the entry that is not a host.
allow_hosts_collect() {
  local rest="$1," part host
  while [[ -n "$rest" ]]; do
    part=${rest%%,*}
    rest=${rest#*,}
    host=$(allow_host_normalise "$part") \
      || die "acquire: --allow-host: \"$part\" is not a host name — give a dotted name such as registry.example.com, with no scheme, path, port, wildcard or space; several go in a comma list or repeated flags."
    ALLOW_HOSTS_REQUESTED+=("$host")
  done
}

# Stores the collected hosts for a slot (deduplicated, in the order given).
allow_hosts_store() {
  local proj="$1" host seen=" "
  (( ${#ALLOW_HOSTS_REQUESTED[@]} > 0 )) || return 0
  mkdir -p "$STATE/allow-hosts"
  : >"$(allow_hosts_file_of "$proj")"
  for host in "${ALLOW_HOSTS_REQUESTED[@]}"; do
    case "$seen" in *" $host "*) continue ;; esac
    seen+="$host "
    printf '%s\n' "$host" >>"$(allow_hosts_file_of "$proj")"
  done
  chmod 600 "$(allow_hosts_file_of "$proj")"
}

# The tokens of a FIREWALL_EXTRA_DOMAINS value, one per line.
allow_hosts_tokens() {
  printf '%s\n' "$1" | tr ', ' '\n\n' | sed '/^$/d'
}

# BASE with the hosts in FILE added (the ones BASE does not already list).
allow_hosts_merge() {
  local base="$1" file="$2" sep=, host token present out="$1"
  [[ "$base" != *" "* || "$base" == *,* ]] || sep=" "
  while IFS= read -r host || [[ -n "$host" ]]; do
    [[ -n "$host" ]] || continue
    present=0
    while IFS= read -r token; do
      [[ "${token,,}" != "$host" ]] || present=1
    done < <(allow_hosts_tokens "$out")
    (( ! present )) || continue
    out="${out:+$out$sep}$host"
  done <"$file"
  printf '%s\n' "$out"
}

# Puts the slot's hosts into its env file, on top of the project's own value.
# Run on every bring-up (ensure_slot_env), so it is idempotent.
sync_slot_allow_hosts() {
  local proj="$1" file base_file env_file base
  file=$(allow_hosts_file_of "$proj")
  [[ -f "$file" ]] || return 0
  env_file=$(slot_env_file_of "$proj")
  [[ -f "$env_file" ]] || return 0
  base_file=$(allow_hosts_base_file_of "$proj")
  if [[ -f "$base_file" ]]; then
    base=$(cat "$base_file")
  else
    base=$(env_value "$env_file" "$ALLOW_HOSTS_VAR")
    printf '%s\n' "$base" >"$base_file"
    chmod 600 "$base_file"
  fi
  upsert_env_var "$env_file" "$ALLOW_HOSTS_VAR" "$(allow_hosts_merge "$base" "$file")"
  chmod 600 "$env_file"
}

# The lease ended: the env value goes back to the project's own and the slot's
# files are removed.
clear_slot_allow_hosts() {
  local proj="$1" env_file base_file base tmp
  env_file=$(slot_env_file_of "$proj")
  base_file=$(allow_hosts_base_file_of "$proj")
  if [[ -f "$base_file" && -f "$env_file" ]]; then
    base=$(cat "$base_file")
    if [[ -n "$base" ]]; then
      upsert_env_var "$env_file" "$ALLOW_HOSTS_VAR" "$base"
    else
      tmp=$(mktemp)
      grep -v "^${ALLOW_HOSTS_VAR}=" "$env_file" >"$tmp" || true
      mv "$tmp" "$env_file"
    fi
    chmod 600 "$env_file"
  fi
  rm -f "$(allow_hosts_file_of "$proj")" "$base_file"
}

# What the slot may reach beyond the base set: the effective list, one per line.
slot_allowed_hosts() {
  local env_file="$STATE/env/$1.env"
  [[ -f "$env_file" ]] || return 0
  allow_hosts_tokens "$(grep "^${ALLOW_HOSTS_VAR}=" "$env_file" | tail -1 | cut -d= -f2- || true)"
}

# The hosts opened for this slot alone, one per line.
slot_extra_hosts() {
  local file; file=$(allow_hosts_file_of "$1")
  [[ -f "$file" ]] && cat "$file"
  return 0
}

allow_hosts_json_array() {
  jq -Rn '[inputs | select(length > 0)]'
}

allow_hosts_join() {
  local out="" line
  while IFS= read -r line; do out="${out:+$out, }$line"; done
  printf '%s\n' "$out"
}

# Printed when a slot is acquired and nothing else of it says so.
allow_hosts_report() {
  local proj="$1"
  [[ -f "$(allow_hosts_file_of "$proj")" ]] || return 0
  echo
  echo "  firewall: opened for this slot only (gone on release): $(slot_extra_hosts "$proj" | allow_hosts_join)"
  echo "  firewall: effective extra hosts (project + this slot): $(slot_allowed_hosts "$proj" | allow_hosts_join)"
}
