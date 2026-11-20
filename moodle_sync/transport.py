"""How a bundle reaches Moodle.

    DockerTransport      the local moodle-docker instance: docker cp + the
                         plugin's CLI import (engine and Moodle on one machine)
    WebServiceTransport  a Moodle on another server: upload + an external
                         function of local_microlearning (not built yet; see
                         moodle_sync/README.md, "Deployment")

Both import either kind of bundle; the plugin reads the kind from the
manifest. ``push`` returns the plugin's result (what was imported, which
learners were updated or skipped, how many refresh requests it answered).
"""

import json
import os
import subprocess
import time

REMOTE_DIR = "/tmp/microlearning_bundle"
IMPORT_SCRIPT = "/var/www/html/public/local/microlearning/cli/import.php"


class TransportError(RuntimeError):
    """The bundle did not reach Moodle, or Moodle refused it."""


class DockerTransport:
    """Copy the bundle into the web container and run the plugin's CLI import."""

    name = "docker"

    def __init__(self, container=None):
        from moodle_setup.docker import CONTAINER

        self.container = container or CONTAINER

    def _docker(self, *args, **kwargs):
        return subprocess.run(["docker", *args], text=True, **kwargs)

    def push(self, bundle_dir, course_id, remote_dir=None):
        """Import ``bundle_dir`` into course ``course_id``; return the plugin's result."""
        # One folder per push: a manual push and the worker can never mix files.
        remote_dir = remote_dir or f"{REMOTE_DIR}_{os.getpid()}_{time.time_ns()}"
        try:
            return self._push(bundle_dir, course_id, remote_dir)
        finally:
            self._docker("exec", self.container, "rm", "-rf", remote_dir, capture_output=True)

    def _push(self, bundle_dir, course_id, remote_dir):
        copied = self._docker("cp", str(bundle_dir), f"{self.container}:{remote_dir}",
                              capture_output=True)
        if copied.returncode != 0:
            raise TransportError(f"docker cp failed: {copied.stderr.strip()}")
        result = self._docker(
            "exec", self.container, "php", IMPORT_SCRIPT,
            f"--dir={remote_dir}", f"--course={course_id}",
            capture_output=True,
        )
        for line in result.stdout.splitlines():
            if not line.startswith("RESULT "):
                print(line)
        if result.returncode != 0:
            raise TransportError(
                (result.stdout + result.stderr).strip()
                or "The Moodle import failed. Is local_microlearning installed and upgraded?"
            )
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        return {}


class WebServiceTransport:
    """A Moodle on another server (deployment; not implemented yet)."""

    name = "webservice"

    def __init__(self, url=None, token=None):
        self.url, self.token = url, token

    def push(self, bundle_dir, course_id):
        raise TransportError(
            "The web-service transport is not built yet (plan step 9). Run the engine "
            "next to the Moodle containers and use --transport docker."
        )


TRANSPORTS = {"docker": DockerTransport, "webservice": WebServiceTransport}


def make_transport(name="docker"):
    try:
        return TRANSPORTS[name]()
    except KeyError:
        raise ValueError(f"Unknown transport {name!r}; choose one of {sorted(TRANSPORTS)}") from None
