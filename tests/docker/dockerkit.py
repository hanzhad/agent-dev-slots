"""Real-docker helpers. These tests run only where DEV_CONTAINER_DOCKER_TESTS=1
and a docker daemon is reachable; the `test` job never sees this directory,
because it has no __init__.py and `unittest discover -s tests` does not descend
into a directory that is not a package."""

import atexit
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import unittest

GUARD = "DEV_CONTAINER_DOCKER_TESTS"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = os.path.join(REPO_ROOT, "scripts", "engine")
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture-image")

needs_docker = unittest.skipUnless(os.environ.get(GUARD) == "1", f"set {GUARD}=1 to run against a real docker")

_IMAGE_CACHE_LOCK = threading.Lock()
_SHARED_ENGINE_IMAGE = None
_SHARED_FIXTURE_IMAGES = {}
_CLEANUP_IMAGES = []
_ATEXIT_REGISTERED = False


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def _cleanup_shared_images():
    global _SHARED_ENGINE_IMAGE
    with _IMAGE_CACHE_LOCK:
        images = list(reversed(_CLEANUP_IMAGES))
        _CLEANUP_IMAGES.clear()
        _SHARED_FIXTURE_IMAGES.clear()
        _SHARED_ENGINE_IMAGE = None
    for image in images:
        docker("rmi", "-f", image, check=False)


def _register_shared_image(image):
    global _ATEXIT_REGISTERED
    if image not in _CLEANUP_IMAGES:
        _CLEANUP_IMAGES.append(image)
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup_shared_images)
        _ATEXIT_REGISTERED = True


def shared_engine_image():
    """Return a process-wide shared engine image tag, registering atexit cleanup."""
    global _SHARED_ENGINE_IMAGE
    with _IMAGE_CACHE_LOCK:
        if _SHARED_ENGINE_IMAGE is None:
            _SHARED_ENGINE_IMAGE = f"dct{secrets.token_hex(3)}-engine:local"
            _register_shared_image(_SHARED_ENGINE_IMAGE)
        return _SHARED_ENGINE_IMAGE


def shared_fixture_image(base_image):
    """Return a cached fixture image built on top of base_image, building once per base."""
    with _IMAGE_CACHE_LOCK:
        cached = _SHARED_FIXTURE_IMAGES.get(base_image)
        if cached is not None:
            if docker("image", "inspect", cached, check=False).returncode == 0:
                return cached
            _SHARED_FIXTURE_IMAGES.pop(base_image, None)
        tag = f"dct{secrets.token_hex(3)}-fixture:local"
        built = docker(
            "build", "-q", "-t", tag, "--build-arg", "BASE=" + base_image, FIXTURE_DIR, check=False
        )
        if built.returncode != 0:
            raise AssertionError(f"docker build fixture image failed: {built.stderr}")
        _SHARED_FIXTURE_IMAGES[base_image] = tag
        _register_shared_image(tag)
        return tag


class RealEngineCase(unittest.TestCase):
    """A fresh engine container per test, sharing built images unless fresh_image = True."""

    fresh_image = False

    def setUp(self):
        self.uid = secrets.token_hex(3)
        self.name = f"dct{self.uid}-engine"
        self.image = f"dct{self.uid}-engine:local" if self.fresh_image else shared_engine_image()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="dct-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.projects = os.path.join(self.tmp, "projects")
        self.state = os.path.join(self.home, ".dev-containers")
        self.limits = os.path.join(self.home, ".config", "machine-limits")
        for path in (self.home, self.projects, self.limits):
            os.makedirs(path)
        write(os.path.join(self.limits, "memory-floor-mb"), "2048\n")
        self.addCleanup(self.cleanup_docker)
        self.install()

    def cleanup_docker(self):
        docker("rm", "-f", self.name, check=False)
        if self.fresh_image:
            docker("rmi", "-f", self.image, check=False)

    def ensure_fixture_image(self):
        if self.fresh_image:
            fixture = f"dct{self.uid}-fixture:local"
            self.addCleanup(docker, "rmi", "-f", fixture, check=False)
            built = docker(
                "build", "-q", "-t", fixture, "--build-arg", "BASE=" + self.image, FIXTURE_DIR, check=False
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            return fixture
        return shared_fixture_image(self.image)

    def env(self, **extra):
        env = dict(os.environ)
        for name in ("DEV_CONTAINER_HOME", "DOCKER_HOST"):
            env.pop(name, None)
        env.update(HOME=self.home, DEV_CONTAINER_ENGINE_NAME=self.name, DEV_CONTAINER_ENGINE_IMAGE=self.image)
        env.update(extra)
        return env

    def script(self, path, *args, cwd=None, env=None):
        return subprocess.run(
            ["/bin/sh", path, *args], cwd=cwd or self.tmp, env=env or self.env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900,
        )

    def install(self):
        result = self.script(ENGINE, "install", "--projects-root", self.projects)
        self.assertEqual(result.returncode, 0, result.stderr)

    def inside(self, *cmd, user=None, workdir=None, check=True):
        flags = (["-u", user] if user else []) + (["-w", workdir] if workdir else [])
        return docker("exec", *flags, self.name, *cmd, check=check)

    def thin(self, *args, cwd):
        return self.script(THIN, *args, cwd=cwd, env=self.env(PWD=cwd))

    def inspect(self, ref=None):
        return json.loads(docker("inspect", ref or self.name).stdout)[0]
