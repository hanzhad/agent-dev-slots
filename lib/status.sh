# shellcheck shell=bash disable=SC2034,SC2154

status_slots_json() {
  local f sid project holder last_activity ports_file
  local -a slot_objs=()
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    [[ -z "$(lease_field "$f" setup)" ]] || continue
    lease_is_live "$f" || continue

    sid=$(basename "$f" .json)
    project=$(lease_field "$f" project)
    [[ -n "$project" ]] || project="$sid"
    holder=$(lease_field "$f" holder)
    last_activity=$(lease_last_activity "$f")

    ports_file="$STATE/ports/$sid.ports"
    local ports_json="{}"
    if [[ -f "$ports_file" ]]; then
      ports_json=$(jq -Rn 'reduce (inputs | select(contains("=")) | split("=")) as $pair ({}; .[$pair[0]] = (if $pair[1] | test("^[0-9]+$") then ($pair[1] | tonumber) else $pair[1] end))' <"$ports_file" 2>/dev/null) || ports_json="{}"
    fi
    local ssh_p
    ssh_p=$(lease_field "$f" ssh_port)
    if [[ "$ports_json" == "{}" && -n "$ssh_p" ]]; then
      ports_json=$(jq -n --argjson p "$ssh_p" '{"SSH_PORT": $p, "ssh_port": $p}')
    elif [[ -n "$ssh_p" ]]; then
      ports_json=$(echo "$ports_json" | jq --argjson p "$ssh_p" '. + (if has("SSH_PORT") then {} else {"SSH_PORT": $p} end) + {"ssh_port": $p}')
    fi

    local obj
    obj=$(jq -n       --arg slot "$sid"       --arg project "$project"       --arg holder "$holder"       --argjson last_activity "$last_activity"       --argjson ports "$ports_json"       '{slot: $slot, project: $project, holder: $holder, last_activity: $last_activity, last_activity_timestamp: $last_activity, ports: $ports}')
    slot_objs+=("$obj")
  done

  if (( ${#slot_objs[@]} == 0 )); then
    echo "[]"
  else
    printf '%s\n' "${slot_objs[@]}" | jq -s '.'
  fi
}

status_cleaners_json() {
  local fast_int slow_int
  fast_int=$(schedule_interval DEV_CONTAINER_SCHEDULE_FAST "$SCHEDULE_FAST_DEFAULT")
  slow_int=$(schedule_interval DEV_CONTAINER_SCHEDULE_SLOW "$SCHEDULE_SLOW_DEFAULT")

  local fast_last="" fast_res=null fast_next=null fast_status="ok"
  if [[ -f "$STATE/schedule/fast.last" ]]; then
    fast_last=$(head -n 1 "$STATE/schedule/fast.last" 2>/dev/null)
  fi
  if [[ "$fast_last" =~ ^[0-9]+$ ]]; then
    fast_next=$(( fast_last + fast_int ))
  else
    fast_last=null
  fi
  if [[ -f "$STATE/schedule/fast.log" ]]; then
    local last_line
    last_line=$(tail -n 1 "$STATE/schedule/fast.log" 2>/dev/null)
    if [[ "$last_line" =~ exited\ 0$ ]]; then
      fast_res='"ok"'
    elif [[ -n "$last_line" ]]; then
      fast_res='"error"'
    fi
  fi

  local slow_last="" slow_res=null slow_next=null slow_status="ok" slow_locked=false
  local FORGE_TOKEN=""
  if ! forge_token_read; then
    slow_status="locked"
    slow_res='"locked"'
    slow_locked=true
  fi

  if [[ -f "$STATE/schedule/slow.last" ]]; then
    slow_last=$(head -n 1 "$STATE/schedule/slow.last" 2>/dev/null)
  fi
  if [[ "$slow_last" =~ ^[0-9]+$ ]]; then
    slow_next=$(( slow_last + slow_int ))
  else
    slow_last=null
  fi
  if [[ "$slow_res" == null && -f "$STATE/schedule/slow.log" ]]; then
    local slow_line
    slow_line=$(tail -n 1 "$STATE/schedule/slow.log" 2>/dev/null)
    if [[ "$slow_line" =~ exited\ 0$ ]]; then
      slow_res='"ok"'
    elif [[ -n "$slow_line" ]]; then
      slow_res='"error"'
    fi
  fi

  jq -n     --argjson fast_last "$fast_last"     --argjson fast_next "$fast_next"     --argjson fast_int "$fast_int"     --argjson fast_res "$fast_res"     --arg fast_status "$fast_status"     --argjson slow_last "$slow_last"     --argjson slow_next "$slow_next"     --argjson slow_int "$slow_int"     --argjson slow_res "$slow_res"     --arg slow_status "$slow_status"     --argjson slow_locked "$slow_locked"     '{
      fast: {
        last_run: $fast_last,
        last_run_timestamp: $fast_last,
        result: $fast_res,
        interval: $fast_int,
        next_run: $fast_next,
        next_planned_run: $fast_next,
        status: $fast_status
      },
      slow: {
        last_run: $slow_last,
        last_run_timestamp: $slow_last,
        result: $slow_res,
        interval: $slow_int,
        next_run: $slow_next,
        next_planned_run: $slow_next,
        status: $slow_status,
        locked: $slow_locked
      }
    } | .fast_clean = .fast | .slow_check = .slow'
}

status_archives_json() {
  local rescue_base="$STATE/rescue"
  local -a archive_objs=()
  local now
  now=$(now_epoch)

  if [[ -d "$rescue_base" ]]; then
    local proj_dir lease_dir
    for proj_dir in "$rescue_base"/*/; do
      [[ -d "$proj_dir" ]] || continue
      proj_dir=${proj_dir%/}
      local project; project=$(basename "$proj_dir")
      for lease_dir in "$proj_dir"/*/; do
        [[ -d "$lease_dir" ]] || continue
        lease_dir=${lease_dir%/}
        local lease; lease=$(basename "$lease_dir")

        local size_bytes=0
        if command -v du >/dev/null 2>&1; then
          size_bytes=$(du -sb "$lease_dir" 2>/dev/null | awk '{print $1}') || size_bytes=0
        fi
        [[ "$size_bytes" =~ ^[0-9]+$ ]] || size_bytes=0

        local verdict=false
        [[ -f "$lease_dir/verdict.md" ]] && verdict=true

        local meta_file="$lease_dir/meta.json"
        local branches="[]"
        local age=0
        local dir_mtime
        dir_mtime=$(stat -c %Y "$lease_dir" 2>/dev/null || stat -f %m "$lease_dir" 2>/dev/null || echo "$now")
        [[ "$dir_mtime" =~ ^[0-9]+$ ]] || dir_mtime=$now
        age=$(( now - dir_mtime ))
        (( age >= 0 )) || age=0

        if [[ -f "$meta_file" ]]; then
          local b_read
          b_read=$(jq -c '.branches // empty' "$meta_file" 2>/dev/null) || b_read=""
          [[ -n "$b_read" && "$b_read" != "null" ]] && branches="$b_read"
          local meta_age
          meta_age=$(jq -r '.age // empty' "$meta_file" 2>/dev/null) || meta_age=""
          if [[ "$meta_age" =~ ^[0-9]+$ ]]; then
            age=$meta_age
          else
            local meta_ts
            meta_ts=$(jq -r '(.timestamp // .created_at // .created) // empty' "$meta_file" 2>/dev/null) || meta_ts=""
            if [[ "$meta_ts" =~ ^[0-9]+$ ]]; then
              age=$(( now - meta_ts ))
              (( age >= 0 )) || age=0
            fi
          fi
        fi

        local obj
        obj=$(jq -n           --arg project "$project"           --arg lease "$lease"           --argjson size "$size_bytes"           --argjson size_bytes "$size_bytes"           --argjson age "$age"           --argjson branches "$branches"           --argjson verdict "$verdict"           --argjson has_verdict "$verdict"           '{
            project: $project,
            lease: $lease,
            size: $size,
            size_bytes: $size_bytes,
            age: $age,
            branches: $branches,
            verdict: $verdict,
            has_verdict: $has_verdict
          }')
        archive_objs+=("$obj")
      done
    done
  fi

  if (( ${#archive_objs[@]} == 0 )); then
    echo "[]"
  else
    printf '%s\n' "${archive_objs[@]}" | jq -s '.'
  fi
}

cmd_status() {
  local json_format=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json_format=1; shift ;;
      *) die "status: unknown argument '$1'" ;;
    esac
  done

  local slots cleaners archives
  slots=$(status_slots_json)
  cleaners=$(status_cleaners_json)
  archives=$(status_archives_json)

  if (( json_format )); then
    jq -n       --argjson slots "$slots"       --argjson cleaners "$cleaners"       --argjson archives "$archives"       '{
        version: 1,
        engine: "running",
        status: "running",
        slots: $slots,
        cleaners: $cleaners,
        archives: $archives
      }'
    return 0
  fi

  echo "Engine: running (version 1)"
  echo ""
  echo "Slots:"
  local slot_count
  slot_count=$(echo "$slots" | jq 'length')
  if (( slot_count == 0 )); then
    echo "  (no live slots)"
  else
    local i
    for (( i=0; i<slot_count; i++ )); do
      local s_proj s_hold s_sid s_act s_ports
      s_sid=$(echo "$slots" | jq -r ".[$i].slot")
      s_proj=$(echo "$slots" | jq -r ".[$i].project")
      s_hold=$(echo "$slots" | jq -r ".[$i].holder")
      s_act=$(echo "$slots" | jq -r ".[$i].last_activity")
      s_ports=$(echo "$slots" | jq -r --argjson idx "$i" '.[$idx].ports | to_entries | map("\(.key) \(.value)") | join(", ")')
      echo "  $s_sid ($s_proj): held by $s_hold, active $(fmt_duration "$(( $(now_epoch) - s_act ))") ago, ports: $s_ports"
    done
  fi
  echo ""
  echo "Cleaners:"
  local fast_last fast_res slow_last slow_res slow_status
  fast_last=$(echo "$cleaners" | jq -r '.fast.last_run // "never"')
  fast_res=$(echo "$cleaners" | jq -r '.fast.result // "none"')
  slow_status=$(echo "$cleaners" | jq -r '.slow.status')
  slow_last=$(echo "$cleaners" | jq -r '.slow.last_run // "never"')
  slow_res=$(echo "$cleaners" | jq -r '.slow.result // "none"')
  echo "  fast clean: last run $fast_last ($fast_res)"
  if [[ "$slow_status" == "locked" ]]; then
    echo "  slow check: locked (no forge token held)"
  else
    echo "  slow check: last run $slow_last ($slow_res)"
  fi
  echo ""
  echo "Archives:"
  local arch_count
  arch_count=$(echo "$archives" | jq 'length')
  if (( arch_count == 0 )); then
    echo "  (no archives)"
  else
    local j
    for (( j=0; j<arch_count; j++ )); do
      local a_proj a_lease a_size a_branches a_verdict
      a_proj=$(echo "$archives" | jq -r ".[$j].project")
      a_lease=$(echo "$archives" | jq -r ".[$j].lease")
      a_size=$(echo "$archives" | jq -r ".[$j].size_bytes")
      a_branches=$(echo "$archives" | jq -r --argjson idx "$j" '.[$idx].branches | join(", ")')
      a_verdict=$(echo "$archives" | jq -r ".[$j].verdict")
      echo "  $a_proj/$a_lease: $a_size bytes, branches: [$a_branches], verdict: $a_verdict"
    done
  fi
  exit 0
}
