import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DOCKER_STUB,
    DevContainerTestCase,
    LIB_REL,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
    project_env_text,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
OVERLAY_LIB = os.path.join(REPO_ROOT, "lib", "overlay.sh")
COMPOSE_LIB = os.path.join(REPO_ROOT, "lib", "compose.sh")
TOOLBOX_LIB = os.path.join(REPO_ROOT, "lib", "toolbox.sh")
TOOLBOX_DOCKERFILE = os.path.join(REPO_ROOT, "image", "toolbox", "Dockerfile")
ENGINE_DOCKERFILE = os.path.join(REPO_ROOT, "image", "Dockerfile")
DOCKERIGNORE = os.path.join(REPO_ROOT, ".dockerignore")

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)
SLOT_TOK = "tok_sentinel_slot_tb1"
CALLER_TOK = "tok_sentinel_caller_tb2"


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def output_of(result):
    return result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")


def _strip_yaml_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _split_top_commas(text):
    items = []
    cur = []
    quote = None
    for ch in text:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def parse_overlay_services(yaml_text):
    """Minimal stdlib parser for compose overlay `services:` blocks.

    Accepts both flow ([a], {k: v}) and block (- a, k: v) YAML syntax for
    service keys, environment, labels, volumes, networks, and build.
    """
    lines = []
    for raw in yaml_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, stripped))

    services = {}
    i = 0
    while i < len(lines):
        indent, text = lines[i]
        if indent == 0 and text == "services:":
            i += 1
            break
        i += 1
    else:
        return services

    if i >= len(lines):
        return services
    svc_indent = lines[i][0]

    while i < len(lines):
        indent, text = lines[i]
        if indent < svc_indent:
            break
        if indent == svc_indent and text.endswith(":"):
            svc_name = text[:-1].strip()
            svc = {}
            i += 1
            if i < len(lines) and lines[i][0] > svc_indent:
                key_indent = lines[i][0]
                while i < len(lines) and lines[i][0] >= key_indent:
                    k_ind, k_text = lines[i]
                    if k_ind != key_indent:
                        i += 1
                        continue
                    key, _, rest = k_text.partition(":")
                    key = key.strip()
                    rest = rest.strip()
                    i += 1
                    children = []
                    while i < len(lines) and lines[i][0] > key_indent:
                        children.append(lines[i])
                        i += 1
                    if rest:
                        if rest.startswith("[") and rest.endswith("]"):
                            svc[key] = [_strip_yaml_quotes(x) for x in _split_top_commas(rest[1:-1])]
                        elif rest.startswith("{") and rest.endswith("}"):
                            mapping = {}
                            for pair in _split_top_commas(rest[1:-1]):
                                mk, _, mv = pair.partition(":")
                                mapping[_strip_yaml_quotes(mk)] = _strip_yaml_quotes(mv)
                            svc[key] = mapping
                        else:
                            svc[key] = _strip_yaml_quotes(rest)
                    elif children:
                        if all(c_text.startswith("- ") or c_text == "-" for _, c_text in children):
                            items = [_strip_yaml_quotes(c_text[1:].strip()) for _, c_text in children]
                            if key == "environment":
                                mapping = {}
                                for item in items:
                                    ek, _, ev = item.partition("=")
                                    mapping[ek] = ev
                                svc[key] = mapping
                            else:
                                svc[key] = items
                        else:
                            sub_indent = children[0][0]
                            mapping = {}
                            raw_lines = []
                            for c_ind, c_text in children:
                                raw_lines.append((c_ind, c_text))
                                if c_ind == sub_indent and ":" in c_text:
                                    mk, _, mv = c_text.partition(":")
                                    mapping[_strip_yaml_quotes(mk)] = _strip_yaml_quotes(mv)
                            svc[key] = mapping if key != "build" else raw_lines
                    else:
                        svc[key] = None
            services[svc_name] = svc
        else:
            i += 1
    return services


TOOLBOX_DOCKER_WRAPPER = r"""#!/usr/bin/env bash
set -u

tb_log="${TOOLBOX_DOCKER_LOG:-/dev/null}"
tb_state="${TOOLBOX_DOCKER_STATE:-/tmp}"
state_dir="${DOCKER_STUB_STATE:-/tmp}"
base="$(dirname "$0")/docker-base"

if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  ref="${@: -1}"
  if [[ "$ref" == *-app:latest && -f "$state_dir/${ref%-app:latest}.image-hash" ]]; then
    exec "$base" "$@"
  fi
  echo "IMAGE_INSPECT $ref" >> "$tb_log"
  if [[ -f "$tb_state/present-images" ]] && grep -Fxq -- "$ref" "$tb_state/present-images"; then
    exit 0
  fi
  exit 1
fi

if [[ "${1:-}" == build ]]; then
  echo "BUILD $*" >> "$tb_log"
  tag="" dockerfile="" ctx="" prev=""
  for arg in "$@"; do
    case "$prev" in
      -t|--tag) tag="$arg" ;;
      -f|--file) dockerfile="$arg" ;;
    esac
    case "$arg" in
      --tag=*) tag="${arg#--tag=}" ;;
      --file=*) dockerfile="${arg#--file=}" ;;
    esac
    prev="$arg"
    ctx="$arg"
  done
  if [[ -n "$dockerfile" && ! -f "$dockerfile" ]]; then
    echo "missing Dockerfile: $dockerfile" >&2
    exit 1
  elif [[ -z "$dockerfile" && -n "$ctx" && ! -f "$ctx/Dockerfile" ]]; then
    echo "missing Dockerfile in context: $ctx" >&2
    exit 1
  fi
  if [[ -f "$tb_state/build-fail" ]]; then
    echo "simulated toolbox docker build failure" >&2
    exit 1
  fi
  if [[ -n "$tag" ]]; then
    mkdir -p "$tb_state"
    echo "$tag" >> "$tb_state/present-images"
  fi
  exit 0
fi

if [[ "${1:-}" == compose ]]; then
  verb_seen="" proj="${COMPOSE_PROJECT_NAME-}" prev=""
  files=()
  i=2
  while (( i <= $# )); do
    arg="${!i}"
    case "$prev" in
      -p|--project-name) proj="$arg" ;;
      -f|--file) files+=("$arg") ;;
    esac
    case "$arg" in
      up|down|stop|port|ps|exec|run|ls|build|config|rm)
        verb_seen="$arg"
        i=$((i + 1))
        break
        ;;
    esac
    prev="$arg"
    i=$((i + 1))
  done

  if [[ "$verb_seen" == config && -z "${DOCKER_STUB_CONFIG_FAIL:-}" && -z "${DOCKER_STUB_CONFIG_JSON:-}" ]]; then
    has_toolbox=0
    for f in ${files[@]+"${files[@]}"}; do
      if [[ -f "$f" ]] && grep -qE '^[[:space:]]+toolbox:[[:space:]]*$' "$f"; then
        has_toolbox=1
        break
      fi
    done
    if (( has_toolbox )); then
      jq -nc '{services: {
        app: {volumes: [{type: "bind", target: "/workspace"}]},
        db: {},
        toolbox: {
          volumes: [{type: "bind", target: "/workspace"}],
          labels: {"dev-container.role": "toolbox"}
        }
      }}'
      exit 0
    fi
  fi

  if [[ "$verb_seen" == exec ]]; then
    exec_user="" exec_wd="" env_flags=() svc="" cmd=() seen_flag_val=0 flag_name=""
    while (( i <= $# )); do
      arg="${!i}"
      if (( seen_flag_val )); then
        case "$flag_name" in
          -u|--user) exec_user="$arg" ;;
          -w|--workdir) exec_wd="$arg" ;;
          -e|--env) env_flags+=("$arg") ;;
        esac
        seen_flag_val=0
        i=$((i + 1))
        continue
      fi
      case "$arg" in
        -u|--user|-w|--workdir|-e|--env)
          seen_flag_val=1
          flag_name="$arg"
          ;;
        --user=*) exec_user="${arg#--user=}" ;;
        --workdir=*) exec_wd="${arg#--workdir=}" ;;
        --env=*) env_flags+=("${arg#--env=}") ;;
        -*)
          ;;
        *)
          svc="$arg"
          i=$((i + 1))
          while (( i <= $# )); do
            cmd+=("${!i}")
            i=$((i + 1))
          done
          break
          ;;
      esac
      i=$((i + 1))
    done

    gh_state="UNSET"
    [[ -n "${GH_TOKEN+x}" ]] && gh_state="SET:${GH_TOKEN}"
    printf 'COMPOSE_EXEC proj=%s svc=%s user=%s workdir=%s env_flags=%s gh=%s cmd=%s\n' \
      "$proj" "$svc" "$exec_user" "$exec_wd" "${env_flags[*]-}" "$gh_state" "${cmd[*]-}" >> "$tb_log"

    if [[ "$svc" == "toolbox" ]]; then
      [[ -n "$proj" && -f "$state_dir/$proj.running" ]] || {
        echo "toolbox container for '$proj' is not running" >&2
        exit 1
      }
      vol_dir="$state_dir/volumes/${SLOT_WORKSPACE_VOLUME:-${proj}-workspace}"
      if [[ -n "${WORKSPACE_DIR:-}" && -d "$WORKSPACE_DIR" ]]; then
        cd "$WORKSPACE_DIR" || exit 1
      elif [[ -d "$vol_dir" ]]; then
        cd "$vol_dir" || exit 1
      fi
      (( ${#cmd[@]} > 0 )) || exit 0
      exec "${cmd[@]}"
    fi
  fi
fi

exec "$base" "$@"
"""


class TestToolboxImageDefinition(unittest.TestCase):
    """Static definition checks for image/toolbox/Dockerfile and image/Dockerfile."""

    def test_toolbox_dockerfile_exists_and_pins_base_playwright_tools_user_and_cmd(self):
        self.assertTrue(os.path.isfile(TOOLBOX_DOCKERFILE), f"missing {TOOLBOX_DOCKERFILE}")
        text = read_text(TOOLBOX_DOCKERFILE)
        lines = text.splitlines()

        froms = [ln.strip() for ln in lines if re.match(r"^\s*FROM\s+", ln, re.IGNORECASE)]
        self.assertEqual(len(froms), 1, f"expected 1 FROM line, got {froms}")
        base_ref = froms[0].split()[1]
        self.assertNotIn(":latest", base_ref, f"base image must be pinned, got {base_ref}")
        self.assertTrue(
            ":" in base_ref or "@sha256:" in base_ref,
            f"base image must carry a pinned tag or digest, got {base_ref}",
        )

        self.assertRegex(
            text,
            r"(?m)PLAYWRIGHT_BROWSERS_PATH\s*=\s*\"?/ms-playwright\"?\b",
            "Dockerfile must set PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
        )
        lower = text.lower()
        self.assertIn("playwright", lower)
        self.assertIn("chromium", lower)
        for tool in ("python3", "jq", "curl", "sqlite3", "postgresql-client"):
            self.assertIn(tool, text, f"toolbox Dockerfile missing required package/tool {tool}")

        self.assertIn("1000", text, "toolbox Dockerfile must configure uid 1000")
        users = [ln.strip() for ln in lines if re.match(r"^\s*USER\s+", ln, re.IGNORECASE)]
        self.assertTrue(users, "toolbox Dockerfile must switch to an unprivileged USER")
        self.assertNotIn(users[-1].split()[1].lower(), ("root", "0", "0:0"))

        self.assertRegex(
            text,
            r'(?m)^\s*(CMD|ENTRYPOINT)\b.*sleep.*infinity',
            "toolbox Dockerfile must default to sleep infinity",
        )

    def test_engine_dockerfile_copies_toolbox_and_dockerignore_allows_it(self):
        text = read_text(ENGINE_DOCKERFILE)
        self.assertRegex(
            text,
            r"(?m)^COPY\s+(--\S+\s+)*image/toolbox/?\s+/opt/dev-container/toolbox/?(\s|$)",
            "image/Dockerfile must bundle image/toolbox into /opt/dev-container/toolbox",
        )
        if os.path.isfile(DOCKERIGNORE):
            ignored = {ln.strip().rstrip("/") for ln in read_text(DOCKERIGNORE).splitlines() if ln.strip()}
            self.assertNotIn("image", ignored)
            self.assertNotIn("image/toolbox", ignored)


class TestEnsureToolboxImage(unittest.TestCase):
    """Tests for ensure_toolbox_image in lib/toolbox.sh with stubbed docker."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tb-ensure-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake_bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.fake_bin)
        self.docker_log = os.path.join(self.tmp, "docker.log")
        self.docker_state = os.path.join(self.tmp, "docker-state")
        os.makedirs(self.docker_state)

        stub = """#!/usr/bin/env bash
set -u
echo "$*" >> "${TB_LOG:?}"
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  ref="${@: -1}"
  [[ -f "${TB_STATE:?}/present" ]] && grep -Fxq -- "$ref" "$TB_STATE/present" && exit 0
  exit 1
fi
if [[ "${1:-}" == build ]]; then
  [[ ! -f "${TB_STATE:?}/fail-build" ]] || exit 1
  tag="" prev=""
  for a in "$@"; do
    [[ "$prev" == "-t" || "$prev" == "--tag" ]] && tag="$a"
    prev="$a"
  done
  [[ -n "$tag" ]] && echo "$tag" >> "$TB_STATE/present"
  exit 0
fi
exit 0
"""
        docker_path = os.path.join(self.fake_bin, "docker")
        write_text(docker_path, stub)
        os.chmod(docker_path, 0o755)

    def _run_lib(self, script, lib_path=TOOLBOX_LIB, **extra_env):
        env = dict(os.environ)
        env["PATH"] = self.fake_bin + os.pathsep + env.get("PATH", "")
        env["TB_LOG"] = self.docker_log
        env["TB_STATE"] = self.docker_state
        env.pop("DEV_CONTAINER_TOOLBOX_IMAGE", None)
        env.update(extra_env)
        return subprocess.run(
            ["bash", "-c", script, "test", lib_path],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )

    def _calls(self):
        if not os.path.isfile(self.docker_log):
            return []
        return [ln for ln in read_text(self.docker_log).splitlines() if ln.strip()]

    def test_ensure_toolbox_image_builds_default_and_custom_tag_only_when_absent(self):
        self.assertTrue(os.path.isfile(TOOLBOX_LIB), f"missing {TOOLBOX_LIB}")
        res = self._run_lib('. "$1" && declare -F ensure_toolbox_image >/dev/null && ensure_toolbox_image')
        self.assertEqual(res.returncode, 0, output_of(res))

        calls = self._calls()
        self.assertTrue(any("image inspect dev-container-toolbox:local" in c for c in calls), calls)
        builds = [c for c in calls if c.startswith("build ")]
        self.assertEqual(len(builds), 1, calls)
        self.assertIn("-t dev-container-toolbox:local", builds[0])
        self.assertIn("toolbox", builds[0])

        res_second = self._run_lib('. "$1" && ensure_toolbox_image')
        self.assertEqual(res_second.returncode, 0, output_of(res_second))
        self.assertEqual(len([c for c in self._calls() if c.startswith("build ")]), 1, self._calls())

        res_custom = self._run_lib(
            '. "$1" && ensure_toolbox_image',
            DEV_CONTAINER_TOOLBOX_IMAGE="custom-toolbox:v9",
        )
        self.assertEqual(res_custom.returncode, 0, output_of(res_custom))
        custom_builds = [c for c in self._calls() if c.startswith("build ") and "custom-toolbox:v9" in c]
        self.assertEqual(len(custom_builds), 1, self._calls())

    def test_ensure_toolbox_image_propagates_build_failure(self):
        self.assertTrue(os.path.isfile(TOOLBOX_LIB), f"missing {TOOLBOX_LIB}")
        write_text(os.path.join(self.docker_state, "fail-build"), "")
        res = self._run_lib('. "$1" && ensure_toolbox_image')
        self.assertNotEqual(res.returncode, 0)

    def test_ensure_toolbox_image_supports_bundled_container_toolbox_dir(self):
        self.assertTrue(os.path.isfile(TOOLBOX_LIB), f"missing {TOOLBOX_LIB}")
        opt_root = os.path.join(self.tmp, "opt-dev-container")
        os.makedirs(os.path.join(opt_root, "lib"))
        shutil.copyfile(TOOLBOX_LIB, os.path.join(opt_root, "lib", "toolbox.sh"))
        shutil.copytree(os.path.join(REPO_ROOT, "image", "toolbox"), os.path.join(opt_root, "toolbox"))

        bundled_lib = os.path.join(opt_root, "lib", "toolbox.sh")
        res = self._run_lib(
            'lib_dir="$(dirname "$1")" && . "$1" && ensure_toolbox_image',
            lib_path=bundled_lib,
        )
        self.assertEqual(res.returncode, 0, output_of(res))
        builds = [c for c in self._calls() if c.startswith("build ")]
        self.assertEqual(len(builds), 1, self._calls())
        self.assertIn(os.path.join(opt_root, "toolbox"), builds[0])


class TestRenderSlotOverlayYamlPureCore(unittest.TestCase):
    """Pure Core unit tests for render_slot_overlay_yaml in lib/overlay.sh (zero Docker).

    Signature expected:
      render_slot_overlay_yaml <proj> <config_json> [limit] [hash] [rev]
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tb-overlay-pure-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "toolbox"), exist_ok=True)
        self.no_docker_bin = os.path.join(self.tmp, "no-docker-bin")
        os.makedirs(self.no_docker_bin)
        docker_bomb = os.path.join(self.no_docker_bin, "docker")
        write_text(docker_bomb, "#!/bin/sh\necho 'docker must not be called by pure renderer' >&2\nexit 99\n")
        os.chmod(docker_bomb, 0o755)

    def _render(self, proj, config_obj, limit="4g", bhash="deadbeef", rev="cafebabe", toolbox_for=(), forge_auth=""):
        for item in toolbox_for:
            write_text(os.path.join(self.state, "toolbox", item), "1\n")

        env = dict(os.environ)
        env["PATH"] = self.no_docker_bin + os.pathsep + env.get("PATH", "")
        env["STATE"] = self.state
        env["FORGE_AUTH"] = forge_auth
        env.pop("PE_SLOT_MEMORY_LIMIT", None)

        probe = subprocess.run(
            ["bash", "-c", '. "$1" && declare -F render_slot_overlay_yaml >/dev/null', "test", OVERLAY_LIB],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(
            probe.returncode,
            0,
            f"render_slot_overlay_yaml is not defined in {OVERLAY_LIB}: {probe.stderr.decode()}",
        )

        res = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1" && render_slot_overlay_yaml "$2" "$3" "$4" "$5" "$6"',
                "test",
                OVERLAY_LIB,
                proj,
                json.dumps(config_obj),
                limit,
                bhash,
                rev,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, output_of(res))
        return res.stdout.decode("utf-8")

    def test_omits_toolbox_unless_exact_slot_marker_present(self):
        cfg = {
            "services": {
                "app": {
                    "volumes": [{"type": "bind", "target": "/workspace"}],
                    "build": {"context": "."},
                }
            }
        }
        yaml_text = self._render(
            "demo-slot-1",
            cfg,
            toolbox_for=["demo-slot-2", "demo-slot-10"],
            forge_auth="token",
        )
        services = parse_overlay_services(yaml_text)
        self.assertIn("app", services)
        self.assertNotIn("toolbox", services)
        self.assertEqual(services["app"].get("mem_limit"), "4g")
        self.assertEqual(services["app"].get("environment", {}).get("GH_TOKEN"), "${SLOT_GH_TOKEN:-}")
        self.assertIn("build", services["app"])

    def test_internal_network_and_https_proxy_fact(self):
        cfg = {
            "services": {
                "app": {
                    "volumes": [{"type": "bind", "target": "/workspace"}],
                    "networks": {"inside": None},
                    "environment": {
                        "HTTPS_PROXY": "http://gateway:8888",
                        "GH_TOKEN": "must-not-leak",
                        "SIGNING_KEY_VOLUME": "must-not-leak-vol",
                    },
                    "build": {"context": "."},
                },
                "db": {"image": "postgres:16"},
            }
        }
        yaml_text = self._render(
            "demo-slot-1",
            cfg,
            limit="2g",
            bhash="0123abcd",
            rev="fedcba98",
            toolbox_for=["demo-slot-1"],
            forge_auth="token",
        )
        services = parse_overlay_services(yaml_text)
        self.assertIn("app", services)
        self.assertIn("toolbox", services)
        self.assertNotIn("db", services)

        tb = services["toolbox"]
        self.assertIn("dev-container-toolbox:local", tb.get("image", ""))
        self.assertEqual(tb.get("init"), "true")
        self.assertEqual(tb.get("shm_size"), "1gb")
        self.assertEqual(tb.get("mem_limit"), "2g")
        self.assertEqual(tb.get("user"), "${CONTAINER_UID:-1000}:${CONTAINER_GID:-1000}")
        self.assertEqual(tb.get("working_dir"), "/workspace")
        self.assertEqual(tb.get("volumes"), ["${WORKSPACE_DIR:-..}:/workspace"])
        self.assertEqual(tb.get("labels", {}).get("dev-container.role"), "toolbox")

        tb_nets = tb.get("networks")
        if isinstance(tb_nets, dict):
            self.assertEqual(list(tb_nets.keys()), ["inside"])
        else:
            self.assertEqual(tb_nets, ["inside"])

        tb_env = tb.get("environment", {})
        self.assertEqual(tb_env.get("HTTPS_PROXY"), "http://gateway:8888")
        self.assertIn("app", [x.strip() for x in tb_env.get("NO_PROXY", "").split(",") if x.strip()])
        self.assertIn("app", [x.strip() for x in tb_env.get("no_proxy", "").split(",") if x.strip()])
        self.assertNotIn("GH_TOKEN", tb_env)
        self.assertNotIn("SLOT_GH_TOKEN", tb_env)
        self.assertNotIn("SIGNING_KEY_VOLUME", tb_env)
        self.assertNotIn("build", tb)
        self.assertNotIn("dev-container.compose-project", str(tb))

        app_env = services["app"].get("environment", {})
        self.assertEqual(app_env.get("GH_TOKEN"), "${SLOT_GH_TOKEN:-}")
        self.assertIn("build", services["app"])

    def test_mirrors_multi_network_all_proxy_vars_and_custom_primary_service(self):
        cfg = {
            "services": {
                "web": {
                    "volumes": [{"type": "bind", "target": "/workspace"}],
                    "networks": {"inside": {"aliases": ["web-alias"]}, "mesh": {}},
                    "environment": {
                        "HTTP_PROXY": "http://proxy:3128",
                        "HTTPS_PROXY": "https://proxy:3129",
                        "http_proxy": "http://proxy:3128",
                        "https_proxy": "https://proxy:3129",
                        "NO_PROXY": "localhost,127.0.0.1",
                        "no_proxy": "localhost,127.0.0.1",
                        "DATABASE_URL": "postgres://secret",
                    },
                },
                "db": {
                    "image": "postgres:16",
                    "networks": {"db_only": None},
                    "environment": {"POSTGRES_PASSWORD": "db-password-value"},
                },
            }
        }
        yaml_text = self._render("demo-slot-1", cfg, toolbox_for=["demo-slot-1"], forge_auth="app")
        services = parse_overlay_services(yaml_text)
        tb = services["toolbox"]

        tb_nets = tb.get("networks")
        net_names = set(tb_nets.keys()) if isinstance(tb_nets, dict) else set(tb_nets or [])
        self.assertEqual(net_names, {"inside", "mesh"})
        self.assertNotIn("web-alias", str(tb_nets))

        tb_env = tb.get("environment", {})
        self.assertEqual(tb_env.get("HTTP_PROXY"), "http://proxy:3128")
        self.assertEqual(tb_env.get("HTTPS_PROXY"), "https://proxy:3129")
        self.assertEqual(tb_env.get("http_proxy"), "http://proxy:3128")
        self.assertEqual(tb_env.get("https_proxy"), "https://proxy:3129")
        for key in ("NO_PROXY", "no_proxy"):
            tokens = [t.strip() for t in tb_env.get(key, "").split(",") if t.strip()]
            self.assertEqual(tokens, ["localhost", "127.0.0.1", "web"], f"{key}={tb_env.get(key)!r}")
        for forbidden in ("GH_TOKEN", "SLOT_GH_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD", "SIGNING_KEY_VOLUME"):
            self.assertNotIn(forbidden, tb_env)

    def test_no_proxy_deduplication_and_no_proxy_stack_omits_proxy_env(self):
        cfg_dedup = {
            "services": {
                "app": {
                    "volumes": [{"type": "bind", "target": "/workspace"}],
                    "environment": {
                        "HTTPS_PROXY": "http://gateway:8888",
                        "NO_PROXY": "localhost,app",
                        "no_proxy": "localhost,app",
                    },
                }
            }
        }
        tb_dedup = parse_overlay_services(
            self._render("demo-slot-1", cfg_dedup, toolbox_for=["demo-slot-1"])
        )["toolbox"]
        for key in ("NO_PROXY", "no_proxy"):
            tokens = [t.strip() for t in tb_dedup["environment"][key].split(",") if t.strip()]
            self.assertEqual(tokens, ["localhost", "app"], f"{key}={tb_dedup['environment'][key]!r}")

        cfg_plain = {
            "services": {
                "app": {
                    "volumes": [{"type": "bind", "target": "/workspace"}],
                    "environment": {"APP_MODE": "dev", "GH_TOKEN": "plain-tok"},
                }
            }
        }
        tb_plain = parse_overlay_services(
            self._render("demo-slot-1", cfg_plain, toolbox_for=["demo-slot-1"], forge_auth="token")
        )["toolbox"]
        env_plain = tb_plain.get("environment", {})
        self.assertEqual(tb_plain.get("networks"), None)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "GH_TOKEN", "SLOT_GH_TOKEN", "APP_MODE"):
            self.assertNotIn(key, env_plain)


class TestWorkspaceServicesAndContainerServiceOf(DevContainerTestCase):
    """Tests for workspace_services_of and container_service_of ignoring toolbox role."""

    def test_workspace_services_of_ignores_toolbox_role_label_across_cases(self):
        cases = [
            (
                "app_and_toolbox",
                {
                    "services": {
                        "app": {"volumes": [{"type": "bind", "target": "/workspace"}]},
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                    }
                },
                "app",
            ),
            (
                "custom_name_null_labels_and_db",
                {
                    "services": {
                        "web": {"volumes": [{"type": "bind", "target": "/workspace"}], "labels": None},
                        "db": {"volumes": [{"type": "volume", "target": "/var/lib/postgresql/data"}]},
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                    }
                },
                "web",
            ),
            (
                "non_toolbox_role_not_ignored",
                {
                    "services": {
                        "app": {"volumes": [{"type": "bind", "target": "/workspace"}]},
                        "worker": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "worker"},
                        },
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                    }
                },
                "app worker",
            ),
            (
                "different_label_key_not_ignored",
                {
                    "services": {
                        "app": {"volumes": [{"type": "bind", "target": "/workspace"}]},
                        "helper": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"role": "toolbox"},
                        },
                    }
                },
                "app helper",
            ),
            (
                "only_toolbox_mounts_workspace",
                {
                    "services": {
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                        "db": {"image": "postgres:16"},
                    }
                },
                "",
            ),
        ]

        self.harness.build_fixture_repo()
        for label, cfg, expected in cases:
            with self.subTest(case=label):
                env = self.harness._env(None)
                env["repo_root"] = self.harness.fixture_repo
                env["COMPOSE_DIR"] = ".devcontainer"
                env["COMPOSE_FILES"] = "docker-compose.yml"
                res = subprocess.run(
                    ["bash", "-c", '. "$1" && workspace_services_of', "test", COMPOSE_LIB],
                    input=json.dumps(cfg).encode("utf-8"),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertEqual(res.returncode, 0, output_of(res))
                self.assertEqual(res.stdout.decode("utf-8").strip(), expected)

    def test_container_service_of_returns_app_when_toolbox_is_active(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        cfg_path = os.path.join(self.harness.tmp, "toolbox-config.json")
        write_text(
            cfg_path,
            json.dumps(
                {
                    "services": {
                        "app": {"volumes": [{"type": "bind", "target": "/workspace"}]},
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                    }
                }
            ),
        )
        sid = f"{PROJECT_PREFIX}-slot-1"
        write_text(os.path.join(self.harness.state_dir, "toolbox", sid), "1\n")

        env = self.harness._env({"DOCKER_STUB_CONFIG_JSON": cfg_path})
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            ["bash", "-c", '. "$1" && container_service_of "" "$2"', "test", ENGINE, sid],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertEqual(res.stdout.decode("utf-8").strip(), "app")

        write_text(
            cfg_path,
            json.dumps(
                {
                    "services": {
                        "app": {"volumes": [{"type": "bind", "target": "/workspace"}]},
                        "worker": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "worker"},
                        },
                        "toolbox": {
                            "volumes": [{"type": "bind", "target": "/workspace"}],
                            "labels": {"dev-container.role": "toolbox"},
                        },
                    }
                }
            ),
        )
        amb = subprocess.run(
            ["bash", "-c", '. "$1" && container_service_of "" "$2"', "test", ENGINE, sid],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertNotEqual(amb.returncode, 0)
        self.assertIn("app, worker", amb.stderr.decode("utf-8"))


class TestToolboxCliAndLifecycle(DevContainerTestCase):
    """CLI wiring tests for acquire/up/run --toolbox and teardown cleanup."""

    def setUp(self):
        super().setUp()
        self.tb_log = os.path.join(self.harness.tmp, "toolbox-docker.log")
        self.tb_state = os.path.join(self.harness.tmp, "toolbox-docker-state")
        os.makedirs(self.tb_state, exist_ok=True)
        self.harness._write_stub("docker-base", DOCKER_STUB)
        self.harness._write_stub("docker", TOOLBOX_DOCKER_WRAPPER)

    def _build_plain_repo(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)

    def _setup_token_auth(self):
        self.harness.build_fixture_repo(
            with_parent_env=f"GH_TOKEN={SLOT_TOK}\n{MAIN_ENV}",
            project_env=project_env_text(FORGE_AUTH="token", FORGE_AUTH_FILE=".devcontainer/.env"),
        )

    def _setup_app_auth(self):
        key_dir = os.path.join(self.harness.tmp, "app-keys")
        os.makedirs(key_dir, exist_ok=True)
        key_path = os.path.join(key_dir, "app.pem")
        subprocess.run(
            ["openssl", "genrsa", "-out", key_path, "2048"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(key_path, 0o600)
        env_text = "\n".join([
            "FORGE_APP_ID=1234567",
            "FORGE_APP_INSTALLATION_ID=7654321",
            "FORGE_APP_SLUG=demo-agent",
            "FORGE_APP_BOT_ID=424242",
            f"FORGE_APP_KEY_FILE={key_path}",
            "FORGE_APP_REPOS=demo demo.docs",
            "",
        ])
        self.harness.build_fixture_repo(
            with_parent_env=env_text,
            project_env=project_env_text(FORGE_AUTH="app", FORGE_AUTH_FILE=".devcontainer/.env"),
        )
        tokens_dir = os.path.join(self.harness.state_dir, "tokens")
        os.makedirs(tokens_dir, mode=0o700, exist_ok=True)
        write_text(os.path.join(tokens_dir, self.harness.project_key), f"{2000000000}\n{SLOT_TOK}\n")

    def _engine(self, *args, extra_env=None, now=None):
        overrides = {
            "TOOLBOX_DOCKER_LOG": self.tb_log,
            "TOOLBOX_DOCKER_STATE": self.tb_state,
        }
        if extra_env:
            overrides.update(extra_env)
        env = self.harness._env(overrides)
        if now is not None:
            env["DEV_CONTAINER_NOW"] = str(now)
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def _tb_lines(self):
        if not os.path.isfile(self.tb_log):
            return []
        return [ln for ln in read_text(self.tb_log).splitlines() if ln.strip()]

    def _clear_tb_log(self):
        if os.path.isfile(self.tb_log):
            os.remove(self.tb_log)

    def _acquired_name(self, result):
        return self.harness.acquired_slot_name(result)

    def _overlay(self, name):
        path = os.path.join(self.harness.state_dir, "overlays", f"{PROJECT_PREFIX}-{name}.yml")
        self.assertTrue(os.path.isfile(path), f"missing overlay {path}")
        return read_text(path)

    def _toolbox_marker(self, name):
        return os.path.join(self.harness.state_dir, "toolbox", f"{PROJECT_PREFIX}-{name}")

    def test_acquire_with_toolbox_builds_lazily_writes_state_renders_overlay_and_runs_toolbox(self):
        self._setup_token_auth()
        since = 1700000000
        self.harness.set_now(since)

        acq = self._engine(
            "acquire", "--toolbox", "--for", "tb-agent", "--as", "Agent [tb1]",
            extra_env={"GH_TOKEN": CALLER_TOK},
        )
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self._acquired_name(acq)
        sid = f"{PROJECT_PREFIX}-{name}"

        self.assertTrue(os.path.isfile(self._toolbox_marker(name)), "toolbox state file missing")
        builds = [ln for ln in self._tb_lines() if ln.startswith("BUILD ")]
        self.assertEqual(len(builds), 1, self._tb_lines())
        self.assertIn("dev-container-toolbox:local", builds[0])

        overlay_services = parse_overlay_services(self._overlay(name))
        self.assertIn("toolbox", overlay_services)
        self.assertNotIn("GH_TOKEN", overlay_services["toolbox"].get("environment", {}))

        status = subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.fixture_repo, "status", "--porcelain", "--untracked-files=no"],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.decode()
        self.assertEqual(status, "")

        self._clear_tb_log()
        self.harness.set_now(since + 50)
        run_res = self._engine(
            "run", name, "--toolbox", "--",
            "sh", "-c", 'printf "%s|%s|%s\\n" "${GH_TOKEN-UNSET}" "$1" "$2"',
            "sh", "a b", "--help",
            extra_env={"GH_TOKEN": CALLER_TOK},
        )
        self.assertEqual(run_res.returncode, 0, output_of(run_res))
        self.assertEqual(run_res.stdout.decode().strip(), "UNSET|a b|--help")

        execs = [ln for ln in self._tb_lines() if ln.startswith("COMPOSE_EXEC ")]
        self.assertEqual(len(execs), 1, self._tb_lines())
        self.assertIn("svc=toolbox ", execs[0])
        self.assertRegex(execs[0], r"\buser=1000(:1000)?\b")
        self.assertNotIn("GH_TOKEN", execs[0].split(" env_flags=", 1)[1].split(" gh=", 1)[0])
        self.assertIn("gh=UNSET ", execs[0])

        act_file = os.path.join(self.harness.state_dir, "activity", f"{sid}.run")
        self.assertTrue(os.path.isfile(act_file))
        self.assertEqual(read_text(act_file).strip(), str(since + 50))

        fail_res = self._engine("run", name, "--toolbox", "--", "sh", "-c", "exit 42", now=since + 60)
        self.assertEqual(fail_res.returncode, 42, output_of(fail_res))
        self.assertEqual(read_text(act_file).strip(), str(since + 60))

    def test_run_toolbox_with_app_auth_never_passes_token_while_ordinary_run_still_does(self):
        self._setup_app_auth()
        acq = self._engine("acquire", "--toolbox", "--for", "tb-app", "--as", "Agent [tb2]")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self._acquired_name(acq)

        self._clear_tb_log()
        tb_res = self._engine(
            "run", name, "--toolbox", "--",
            "sh", "-c", 'printf "%s\\n" "${GH_TOKEN-UNSET}"',
            extra_env={"GH_TOKEN": CALLER_TOK},
        )
        self.assertEqual(tb_res.returncode, 0, output_of(tb_res))
        self.assertEqual(tb_res.stdout.decode().strip(), "UNSET")
        execs = [ln for ln in self._tb_lines() if ln.startswith("COMPOSE_EXEC ")]
        self.assertEqual(len(execs), 1, execs)
        self.assertIn("svc=toolbox ", execs[0])
        self.assertIn("gh=UNSET ", execs[0])

        self._clear_tb_log()
        app_res = self._engine("run", name, "--", "sh", "-c", "true")
        self.assertEqual(app_res.returncode, 0, output_of(app_res))
        app_execs = [ln for ln in self._tb_lines() if ln.startswith("COMPOSE_EXEC ")]
        self.assertEqual(len(app_execs), 1, app_execs)
        self.assertIn("svc=app ", app_execs[0])
        self.assertIn(f"gh=SET:{SLOT_TOK}", app_execs[0])

    def test_acquire_without_toolbox_skips_image_and_state(self):
        self._build_plain_repo()
        acq = self._engine("acquire", "--for", "plain-holder", "--as", "Agent [p1]")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self._acquired_name(acq)

        self.assertFalse(os.path.exists(self._toolbox_marker(name)))
        self.assertEqual([ln for ln in self._tb_lines() if ln.startswith("BUILD ")], [])
        self.assertEqual([ln for ln in self._tb_lines() if ln.startswith("IMAGE_INSPECT ")], [])
        self.assertNotIn("toolbox", parse_overlay_services(self._overlay(name)))

    def test_acquire_toolbox_skips_build_when_present_handles_build_fail_and_ephemeral(self):
        self._build_plain_repo()
        write_text(os.path.join(self.tb_state, "present-images"), "custom-tb:v3\n")
        acq1 = self._engine(
            "acquire", "--toolbox", "--ephemeral", "--for", "eph-tb", "--as", "Agent [e1]",
            extra_env={"DEV_CONTAINER_TOOLBOX_IMAGE": "custom-tb:v3"},
        )
        self.assertEqual(acq1.returncode, 0, output_of(acq1))
        name1 = self._acquired_name(acq1)
        self.assertEqual([ln for ln in self._tb_lines() if ln.startswith("BUILD ")], [])
        self.assertTrue(os.path.isfile(self._toolbox_marker(name1)))
        with open(self.harness.lease_path(name1), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertIs(lease.get("ephemeral"), True)

        write_text(os.path.join(self.tb_state, "build-fail"), "")
        acq_fail = self._engine(
            "acquire", "--toolbox", "--for", "fail-tb", "--as", "Agent [e2]",
            extra_env={"DEV_CONTAINER_TOOLBOX_IMAGE": "missing-tb:v1"},
        )
        self.assertNotEqual(acq_fail.returncode, 0)
        self.assertEqual(len(os.listdir(os.path.join(self.harness.state_dir, "leases"))), 1)
        self.assertFalse(os.path.exists(self._toolbox_marker("slot-2")))

    def test_run_toolbox_refuses_cleanly_without_toolbox_and_invalid_states(self):
        self._build_plain_repo()
        limits_path = os.path.join(self.harness.state_dir, "limits.json")
        write_text(limits_path, json.dumps({"machine_max": 20, "idle_limit": 60}))
        since = 1700000000
        self.harness.set_now(since)

        acq = self._engine("acquire", "--for", "plain-h", "--as", "Agent [r1]")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self._acquired_name(acq)

        self._clear_tb_log()
        no_tb = self._engine("run", name, "--toolbox", "--", "sh", "-c", "echo should-not-run")
        self.assertNotEqual(no_tb.returncode, 0)
        self.assertNotIn("should-not-run", no_tb.stdout.decode())
        err = no_tb.stderr.decode()
        self.assertNotIn("Usage:", err)
        self.assertNotIn("needs '--'", err)
        self.assertNotIn("unknown", err.lower())
        self.assertIn("--toolbox", err)
        self.assertEqual([ln for ln in self._tb_lines() if ln.startswith("COMPOSE_EXEC ")], [])

        up_tb = self._engine("up", name, "--toolbox")
        self.assertEqual(up_tb.returncode, 0, output_of(up_tb))

        self.harness.mark_stopped(name)
        stopped = self._engine("run", name, "--toolbox", "--", "true")
        self.assertNotEqual(stopped.returncode, 0)
        self.harness.mark_running(name)

        expired = self._engine("run", name, "--toolbox", "--", "true", now=since + 61)
        self.assertNotEqual(expired.returncode, 0)

        self.harness.set_now(since + 10)
        bad_combo = self._engine("run", name, "--toolbox", "--live", "FOO", "--", "true")
        self.assertNotEqual(bad_combo.returncode, 0)
        bad_combo2 = self._engine("run", name, "--live", "FOO", "--toolbox", "--", "true")
        self.assertNotEqual(bad_combo2.returncode, 0)

        missing_dash = self._engine("run", name, "--toolbox", "true")
        self.assertNotEqual(missing_dash.returncode, 0)
        missing_cmd = self._engine("run", name, "--toolbox", "--")
        self.assertNotEqual(missing_cmd.returncode, 0)
        missing_slot = self._engine("run", "slot-99", "--toolbox", "--", "true")
        self.assertNotEqual(missing_slot.returncode, 0)

        parked = self._engine("park", name)
        self.assertEqual(parked.returncode, 0, output_of(parked))
        parked_run = self._engine("run", name, "--toolbox", "--", "true")
        self.assertNotEqual(parked_run.returncode, 0)

        unpark = self._engine("park", name, "--clear")
        self.assertEqual(unpark.returncode, 0, output_of(unpark))
        unleased_run = self._engine("run", name, "--toolbox", "--", "true")
        self.assertNotEqual(unleased_run.returncode, 0)

    def test_up_with_toolbox_enables_sidecar_combines_with_secrets_and_restart_preserves(self):
        self._build_plain_repo()
        acq = self._engine("acquire", "--for", "up-holder", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self._acquired_name(acq)

        bad_slot = self._engine("up", "slot-99", "--toolbox")
        self.assertNotEqual(bad_slot.returncode, 0)
        self.assertFalse(os.path.exists(self._toolbox_marker("slot-99")))
        self.assertEqual([ln for ln in self._tb_lines() if ln.startswith("BUILD ")], [])

        up_tb = self._engine("up", name, "--toolbox")
        self.assertEqual(up_tb.returncode, 0, output_of(up_tb))
        self.assertTrue(os.path.isfile(self._toolbox_marker(name)))
        self.assertIn("toolbox", parse_overlay_services(self._overlay(name)))
        self.assertEqual(len([ln for ln in self._tb_lines() if ln.startswith("BUILD ")]), 1)

        ran = self._engine("run", name, "--toolbox", "--", "sh", "-c", "echo enabled-via-up")
        self.assertEqual(ran.returncode, 0, output_of(ran))
        self.assertEqual(ran.stdout.decode().strip(), "enabled-via-up")

        write_text(os.path.join(self.harness.main_devcontainer(), "docker-compose.secrets.yml"), "# secrets\n")
        write_text(os.path.join(self.harness.slot_dir(name), ".env.test.local"), "DUMMY_KEY=val\n")
        up_both = self._engine("up", name, "--toolbox", "--secrets", "--for", "up-holder")
        self.assertEqual(up_both.returncode, 0, output_of(up_both))
        self.assertTrue(os.path.isfile(self._toolbox_marker(name)))
        self.assertIn("toolbox", parse_overlay_services(self._overlay(name)))
        up_calls = [c for c in self.harness.compose_calls() if c["verb"] == "up"]
        self.assertTrue(any("secrets" in os.path.basename(f) for f in up_calls[-1]["files"]))

        rst = self._engine("restart", name)
        self.assertEqual(rst.returncode, 0, output_of(rst))
        self.assertTrue(os.path.isfile(self._toolbox_marker(name)))
        self.assertIn("toolbox", parse_overlay_services(self._overlay(name)))

    def test_teardown_release_and_down_remove_toolbox_state_and_reset_reuse(self):
        self._build_plain_repo()
        acq1 = self._engine("acquire", "--toolbox", "--for", "rel-h", "--as", "Agent [td1]")
        self.assertEqual(acq1.returncode, 0, output_of(acq1))
        name1 = self._acquired_name(acq1)
        marker1 = self._toolbox_marker(name1)
        self.assertTrue(os.path.isfile(marker1))

        rel = self._engine("release", name1, "--for", "rel-h")
        self.assertEqual(rel.returncode, 0, output_of(rel))
        self.assertFalse(os.path.exists(marker1), "release did not remove toolbox state")

        reacq = self._engine("acquire", "--for", "plain-reacq", "--as", "Agent [td2]")
        self.assertEqual(reacq.returncode, 0, output_of(reacq))
        reacq_name = self._acquired_name(reacq)
        self.assertEqual(reacq_name, name1)
        self.assertFalse(os.path.exists(self._toolbox_marker(reacq_name)))
        self.assertNotIn("toolbox", parse_overlay_services(self._overlay(reacq_name)))

        up_tb = self._engine("up", reacq_name, "--toolbox")
        self.assertEqual(up_tb.returncode, 0, output_of(up_tb))
        self.assertTrue(os.path.isfile(self._toolbox_marker(reacq_name)))
        down_res = self._engine("down", reacq_name, "--force")
        self.assertEqual(down_res.returncode, 0, output_of(down_res))
        self.assertFalse(os.path.exists(self._toolbox_marker(reacq_name)), "down did not remove toolbox state")

        sid_direct = f"{PROJECT_PREFIX}-slot-7"
        direct_marker = os.path.join(self.harness.state_dir, "toolbox", sid_direct)
        write_text(direct_marker, "1\n")
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        td = subprocess.run(
            ["bash", "-c", '. "$1" && teardown_dir "" "$2"', "test", ENGINE, sid_direct],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(td.returncode, 0, output_of(td))
        self.assertFalse(os.path.exists(direct_marker), "teardown_dir did not remove toolbox state")

    def test_help_mentions_toolbox_and_slot_compose_stays_sole_compose_caller(self):
        self._build_plain_repo()
        for verb in ("acquire", "up", "run"):
            res = self._engine(verb, "--help")
            self.assertEqual(res.returncode, 0, output_of(res))
            self.assertIn("--toolbox", res.stdout.decode(), f"{verb} --help missing --toolbox")

        paths = [ENGINE]
        lib_dir = os.path.join(REPO_ROOT, LIB_REL)
        paths += [os.path.join(lib_dir, name) for name in sorted(os.listdir(lib_dir)) if name.endswith(".sh")]
        sites = []
        for path in paths:
            for idx, line in enumerate(read_text(path).splitlines()):
                if not line.lstrip().startswith("#") and re.search(r"(^|[\s(])docker compose(\s|$)", line):
                    sites.append((path, idx, line))
        self.assertEqual(len(sites), 1, f"expected single docker compose site in slot_compose, got {sites}")
        self.assertTrue(sites[0][0].endswith(os.path.join("lib", "compose.sh")), sites)


if __name__ == "__main__":
    unittest.main()
