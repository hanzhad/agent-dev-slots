# shellcheck shell=bash disable=SC2034,SC2154

DOCS_PLACEHOLDER_URL="https://github.com/example/demo.docs.git"

repo_url_normalize() {
  local u="$1"
  u=${u%/}
  u=${u%.git}
  u=${u%/}
  printf '%s' "$u"
}

repo_github_slug() {
  local u="$1"
  if [[ "$u" =~ ^(https?://|ssh://)?(git@)?github\.com[:/]([^/]+/[^/]+)$ ]]; then
    local rest="${BASH_REMATCH[3]}"
    rest=${rest%/}
    rest=${rest%.git}
    rest=${rest%/}
    if [[ "$rest" == */* && "$rest" != */*/* ]]; then
      printf '%s' "$rest"
      return 0
    fi
  fi
  printf ''
}

slot_repos_catalog_jq() {
  cat <<'JQ'
def has_ws_or_ctrl: test("[[:cntrl:][:space:]]");
def norm_url: sub("/+$"; "") | sub("\\.git$"; "") | sub("/+$"; "");
def valid_rel_path($fld; $ctx):
  if (type != "string") or (length == 0) then
    error("\($ctx): \($fld) must be a non-empty string")
  elif has_ws_or_ctrl then
    error("\($ctx): \($fld) '\(.)' must not contain whitespace or control characters")
  elif startswith("/") or endswith("/") then
    error("\($ctx): \($fld) '\(.)' must be checkout-relative with no leading or trailing '/'")
  elif (split("/") | any(. == "" or . == "." or . == "..")) then
    error("\($ctx): \($fld) '\(.)' must not contain '.', '..' or empty path segments")
  elif (. == ".git") or startswith(".git/") then
    error("\($ctx): \($fld) '\(.)' cannot target .git")
  else . end;

if type != "object" then
  error("catalog root must be a JSON object")
elif (has("version") | not) or ((.version | type) != "number") or (.version != 1) then
  error("catalog requires integer version: 1")
elif (has("repos") | not) or ((.repos | type) != "array") then
  error("catalog requires repos array")
else
  .repos as $raw
  | [ range(0; ($raw | length)) as $i
      | $raw[$i] as $r
      | if ($r | type) != "object" then
          error("repos[\($i)] must be an object")
        else
          ($r.alias // null) as $alias
          | if ($alias | type) != "string" or ($alias | test("^[a-z0-9][a-z0-9_-]*$") | not) then
              error("repos[\($i)] invalid alias '\($alias)': must match ^[a-z0-9][a-z0-9_-]*$")
            elif $alias == "root" or $alias == "." or $alias == "all" then
              error("repos[\($i)] alias '\($alias)' is reserved")
            else . end
          | ($r.path // null) as $path
          | ($path | valid_rel_path("path"; "repo '\($alias)'"))
          | if ($compose_dir != "" and $compose_dir != "." and ($path == $compose_dir or ($path | startswith($compose_dir + "/")))) then
              error("repo '\($alias)' path '\($path)' cannot target COMPOSE_DIR '\($compose_dir)'")
            else . end
          | ($r.url // null) as $url
          | if ($url | type) != "string" or ($url | length) == 0 then
              error("repo '\($alias)' requires non-empty url")
            elif ($url | has_ws_or_ctrl) then
              error("repo '\($alias)' url '\($url)' must not contain whitespace or control characters")
            elif ($url | startswith("-")) then
              error("repo '\($alias)' url '\($url)' must not start with '-'")
            else . end
          | ($r.mode // null) as $mode
          | if $mode != "ro" and $mode != "rw" then
              error("repo '\($alias)' invalid mode '\($mode)': must be \"ro\" or \"rw\"")
            else . end
          | ($r.attach // null) as $attach
          | if $attach != "default" and $attach != "optional" then
              error("repo '\($alias)' invalid attach '\($attach)': must be \"default\" or \"optional\"")
            else . end
          | (if ($r | has("branch")) and $r.branch != null then
               if ($r.branch | type) != "string" or ($r.branch | length) == 0 or ($r.branch | startswith("-")) or ($r.branch | has_ws_or_ctrl) then
                 error("repo '\($alias)' invalid branch '\($r.branch)'")
               else $r.branch end
             else null end) as $branch
          | (if ($r | has("pin_file")) and $r.pin_file != null then
               if $mode != "ro" then
                 error("repo '\($alias)' pin_file is only allowed when mode is \"ro\" (not \"rw\")")
               elif $branch != null then
                 error("repo '\($alias)' branch and pin_file are mutually exclusive")
               else ($r.pin_file | valid_rel_path("pin_file"; "repo '\($alias)'")) end
             else null end) as $pin_file
          | (if ($r | has("depth")) and $r.depth != null then
               if $r.depth == "full" then "full"
               elif (($r.depth | type) == "number") and (($r.depth | floor) == $r.depth) and ($r.depth >= 1) then $r.depth
               else error("repo '\($alias)' invalid depth '\($r.depth)': must be a positive integer or \"full\"") end
             else (if $mode == "ro" then 1 else "full" end) end) as $depth
          | (if ($r | has("auth")) and $r.auth != null then
               if $r.auth != "project" and $r.auth != "none" then
                 error("repo '\($alias)' invalid auth '\($r.auth)': must be \"project\" or \"none\"")
               elif $r.auth == "none" and $mode != "ro" then
                 error("repo '\($alias)' auth \"none\" is only allowed when mode is \"ro\" (not \"rw\")")
               else $r.auth end
             else "project" end) as $auth
          | (if ($r | has("rw_alias")) and $r.rw_alias != null then
               if ($r.rw_alias | type) != "string" or ($r.rw_alias | length) == 0 then
                 error("repo '\($alias)' invalid rw_alias")
               else $r.rw_alias end
             else null end) as $rw_alias
          | {
              alias: $alias,
              path: $path,
              url: $url,
              mode: $mode,
              attach: $attach,
              depth: $depth,
              auth: $auth
            }
            + (if $branch != null then {branch: $branch} else {} end)
            + (if $pin_file != null then {pin_file: $pin_file} else {} end)
            + (if $rw_alias != null then {rw_alias: $rw_alias} else {} end)
        end
    ] as $repos
  | (reduce $repos[] as $r ({}; if has($r.alias) then error("duplicate repo alias '\($r.alias)': aliases must be unique") else . + {($r.alias): $r} end)) as $by_alias
  | ($repos | map(.path)) as $paths
  | ([ range(0; ($paths | length)) as $i
       | range($i + 1; ($paths | length)) as $j
       | $paths[$i] as $a
       | $paths[$j] as $b
       | if $a == $b or ($b | startswith($a + "/")) or ($a | startswith($b + "/")) then
           error("repo path conflict between '\($a)' and '\($b)': paths must not be equal, an ancestor, or a descendant")
         else empty end
     ]) as $_path_check
  | ([ $repos[]
       | select(.rw_alias != null)
       | . as $src
       | ($by_alias[$src.rw_alias] // null) as $tgt
       | if $tgt == null then
           error("repo '\($src.alias)' rw_alias '\($src.rw_alias)' does not exist in catalog")
         elif $tgt.mode != "rw" then
           error("repo '\($src.alias)' rw_alias '\($src.rw_alias)' has mode '\($tgt.mode)', expected \"rw\"")
         elif ($src.url | norm_url) != ($tgt.url | norm_url) then
           error("repo '\($src.alias)' rw_alias '\($src.rw_alias)' url '\($tgt.url)' does not match '\($src.url)'")
         else empty end
     ]) as $_rw_check
  | { version: 1, repos: $repos }
end
JQ
}

slot_repos_catalog_json() {
  local root="${1:-${repo_root:-}}"
  local repos_file="${2-${SLOT_REPOS_FILE:-}}"
  local docs_url="${3-${DOCS_REPO_URL:-}}"
  local docs_pin="${4-${DOCS_PIN_FILE:-.github/docs-pin}}"
  local compose_dir="${5-${COMPOSE_DIR:-.devcontainer}}"
  local nl=$'\n' cr=$'\r'

  [[ -n "$docs_pin" ]] || docs_pin=".github/docs-pin"
  compose_dir=${compose_dir%/}

  if [[ -n "$repos_file" && -n "$docs_url" && "$docs_url" != "$DOCS_PLACEHOLDER_URL" ]]; then
    die "cannot set both SLOT_REPOS_FILE ($repos_file) and DOCS_REPO_URL ($docs_url) — migrate DOCS_REPO_URL into $repos_file and remove DOCS_REPO_URL."
  fi

  if [[ -n "$repos_file" ]]; then
    case "$repos_file" in
      /*|*/|.|..|../*|*/..|*/../*|./*|*/.|*/./*|*[[:space:]]*|*["$nl$cr"]*)
        die "SLOT_REPOS_FILE '$repos_file' is not a valid checkout-relative file path"
        ;;
    esac
    [[ -n "$root" ]] || die "SLOT_REPOS_FILE: checkout root is unknown"
    local full="$root/$repos_file"
    [[ -f "$full" ]] || die "SLOT_REPOS_FILE '$repos_file' not found at $full"

    local jq_prog out err rc=0 branches branch checked
    jq_prog=$(slot_repos_catalog_jq)
    err=$(mktemp)
    out=$(jq -c --arg compose_dir "$compose_dir" "$jq_prog" "$full" 2>"$err") || rc=$?
    if (( rc != 0 )); then
      local msg
      msg=$(sed -E 's/^jq: error \([^)]*\): //' "$err" | head -n 1)
      rm -f "$err"
      die "SLOT_REPOS_FILE ($repos_file): ${msg:-invalid JSON}"
    fi
    rm -f "$err"

    branches=$(printf '%s' "$out" | jq -r '.repos[] | .branch // empty')
    while IFS= read -r branch; do
      [[ -n "$branch" ]] || continue
      [[ "$branch" != -* ]] || die "SLOT_REPOS_FILE ($repos_file): invalid branch '$branch'"
      checked=$(git check-ref-format --branch "$branch" 2>/dev/null) || checked=""
      [[ "$checked" == "$branch" ]] || die "SLOT_REPOS_FILE ($repos_file): branch '$branch' is not a valid git branch name"
    done <<<"$branches"

    printf '%s\n' "$out"
    return 0
  fi

  if [[ -n "$docs_url" && "$docs_url" != "$DOCS_PLACEHOLDER_URL" ]]; then
    jq -nc --arg url "$docs_url" --arg pin "$docs_pin" '{
      version: 1,
      repos: [
        {
          alias: "docs",
          path: "docs/repo",
          url: $url,
          mode: "ro",
          attach: "default",
          pin_file: $pin,
          depth: 1,
          auth: "project",
          rw_alias: "docs-fix",
          legacy_docs: true
        },
        {
          alias: "docs-fix",
          path: "docs/fix",
          url: $url,
          mode: "rw",
          attach: "optional",
          depth: "full",
          auth: "project",
          legacy_docs: true
        }
      ]
    }'
    return 0
  fi

  printf '{"version":1,"repos":[]}\n'
}

COLLECTED_REPO_ALIASES=()

slot_repos_collect_aliases() {
  local raw="$1" rest part
  [[ -n "$raw" ]] || die "--repo needs a non-empty alias (or comma-separated aliases)"
  rest="$raw,"
  while [[ -n "$rest" ]]; do
    part=${rest%%,*}
    rest=${rest#*,}
    [[ -n "$part" ]] || die "--repo '$raw' contains an empty alias"
    COLLECTED_REPO_ALIASES+=("$part")
  done
}

# Validate requested aliases (if any) against catalog JSON, and return JSON array
# of selected repos (default repos + requested optional repos, or all if all_flag=1).
slot_repos_select_for_acquire() {
  local catalog="$1" all_flag="${2:-0}"
  shift 2
  local alias found
  for alias in "$@"; do
    [[ "$alias" != "root" && "$alias" != "." && "$alias" != "all" ]] \
      || die "unknown repo alias '$alias': '$alias' is reserved"
    found=$(printf '%s' "$catalog" | jq -r --arg a "$alias" '[.repos[] | select(.alias == $a)] | length')
    (( found > 0 )) || die "unknown repo alias '$alias' — not defined in repo catalog"
  done
  local req_json="[]"
  if (( $# > 0 )); then
    req_json=$(jq -nc '$ARGS.positional' --args "$@")
  fi
  printf '%s' "$catalog" | jq -c --argjson all "$all_flag" --argjson req "$req_json" '
    [.repos[] | select($all == 1 or .attach == "default" or (.alias as $a | $req | index($a) != null))]
  '
}

# Validate requested aliases for `attach` (must all exist in catalog) and return
# JSON array of ONLY the requested repos (deduplicated, preserving catalog order).
slot_repos_select_for_attach() {
  local catalog="$1"
  shift
  (( $# > 0 )) || die "attach requires --repo <alias>[,<alias>]..."
  local alias found
  for alias in "$@"; do
    [[ "$alias" != "root" && "$alias" != "." && "$alias" != "all" ]] \
      || die "unknown repo alias '$alias': '$alias' is reserved"
    found=$(printf '%s' "$catalog" | jq -r --arg a "$alias" '[.repos[] | select(.alias == $a)] | length')
    (( found > 0 )) || die "unknown repo alias '$alias' — not defined in repo catalog"
  done
  local req_json
  req_json=$(jq -nc '$ARGS.positional' --args "$@")
  printf '%s' "$catalog" | jq -c --argjson req "$req_json" '
    [.repos[] | select(.alias as $a | $req | index($a) != null)]
  '
}

slot_repos_to_state_entries() {
  local repos_json="$1" alias path url mode attach slug row
  local -a objs=()
  while IFS=$'\t' read -r alias path url mode attach; do
    [[ -n "$alias" ]] || continue
    slug=$(repo_github_slug "$url")
    row=$(jq -nc --arg alias "$alias" --arg path "$path" --arg slug "$slug" --arg mode "$mode" --arg attach "$attach" \
      '{alias: $alias, path: $path, slug: $slug, mode: $mode, attach: $attach}')
    objs+=("$row")
  done < <(printf '%s' "$repos_json" | jq -r '.[] | [.alias, .path, .url, .mode, .attach] | @tsv')
  if (( ${#objs[@]} == 0 )); then
    printf '[]\n'
  else
    printf '%s\n' "${objs[@]}" | jq -cs '.'
  fi
}

slot_repos_state_file_of() { echo "$STATE/repos/$1.json"; }

slot_repos_read_state() {
  local sid="$1" file lease
  file=$(slot_repos_state_file_of "$sid")
  if [[ -f "$file" ]]; then
    jq -c 'if type == "array" then . else [] end' "$file" 2>/dev/null || printf '[]\n'
    return 0
  fi
  lease=$(lease_file_of "$sid")
  if [[ -f "$lease" ]]; then
    jq -c '.repos // []' "$lease" 2>/dev/null || printf '[]\n'
    return 0
  fi
  printf '[]\n'
}

slot_repos_write_state() {
  local sid="$1" entries_json="$2" file
  file=$(slot_repos_state_file_of "$sid")
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$entries_json" | jq -c '.' >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
}

slot_repos_merge_state() {
  local sid="$1" new_entries_json="$2" existing merged
  existing=$(slot_repos_read_state "$sid")
  merged=$(jq -nc --argjson a "$existing" --argjson b "$new_entries_json" '
    reduce ($a + $b)[] as $item (
      {seen: {}, out: []};
      if .seen[$item.alias] then . else .seen[$item.alias] = true | .out += [$item] end
    ) | .out
  ')
  slot_repos_write_state "$sid" "$merged"
  printf '%s\n' "$merged"
}

slot_repos_sync_lease() {
  local sid="$1" entries_json="$2" lease
  lease=$(lease_file_of "$sid")
  [[ -f "$lease" ]] || return 0
  jq --argjson repos "$entries_json" '.repos = $repos' "$lease" >"$lease.tmp.$$"
  mv "$lease.tmp.$$" "$lease"
}

slot_repos_check_untracked() {
  local root="$1" catalog="$2" path tracked
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    tracked=$(git -C "$root" ls-files -- "$path" 2>/dev/null || true)
    if [[ -n "$tracked" ]]; then
      die "refusing slot creation: catalog repo path '$path' is already tracked in the primary repository"
    fi
  done < <(printf '%s' "$catalog" | jq -r 'if type == "array" then .[].path else .repos[].path end')
}

slot_repos_paths_nl() {
  local catalog="$1"
  printf '%s' "$catalog" | jq -r '.repos[].path'
}

slot_repos_exclude_in_dir() {
  local dir="$1" catalog="$2" path
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    ensure_excluded "$dir" "/$path"
  done < <(slot_repos_paths_nl "$catalog")
}

slot_repos_all_present_paths() {
  local target="$1" catalog="${2:-}" sid="" state_file path seen=" "
  if [[ "$target" == /* ]]; then
    sid=$(slot_id_of "$target" 2>/dev/null || true)
  else
    sid="$target"
  fi
  if [[ -n "$catalog" ]]; then
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      case "$seen" in *" $path "*) continue ;; esac
      seen+="$path "
      printf '%s\n' "$path"
    done < <(slot_repos_paths_nl "$catalog")
  fi
  if [[ -n "$sid" ]]; then
    state_file=$(slot_repos_state_file_of "$sid")
    if [[ -f "$state_file" ]]; then
      while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        case "$seen" in *" $path "*) continue ;; esac
        seen+="$path "
        printf '%s\n' "$path"
      done < <(jq -r '.[].path // empty' "$state_file" 2>/dev/null || true)
    fi
  fi
  for path in docs/repo docs/fix; do
    case "$seen" in *" $path "*) continue ;; esac
    seen+="$path "
    printf '%s\n' "$path"
  done
}
