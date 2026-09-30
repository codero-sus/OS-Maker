"""Integration-style tests that drive the whole build pipeline with a fake ``lb``.

A stub ``lb`` stands in for live-build, so the tests exercise workspace setup,
``apply-staging.sh``, ISO discovery, checksumming and log collection without
Docker, root privileges or network access.

Run from the repository root::

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
import unittest.mock
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_os

FAKE_LB = """\
#!/usr/bin/env bash
# Minimal live-build stand-in used by the tests.
set -eu
case "$1" in
  config)
    mkdir -p config/package-lists config/hooks/normal config/includes.chroot
    printf '%s\\n' "$*" > config/last-config.txt
    ;;
  build)
    if [ -n "${FAKE_LB_FAIL:-}" ]; then
        echo "fake lb: build blew up" >&2
        exit 17
    fi
    echo "fake lb: building an image"
    printf 'ISO9660 fake image data\\n' > live-image-amd64.hybrid.iso
    ;;
  *)
    echo "fake lb: unexpected call: $*" >&2
    exit 3
    ;;
esac
"""

NO_OP_LB = """\
#!/usr/bin/env bash
# 'lb config' works, 'lb build' produces nothing (a silently failed build).
set -eu
case "$1" in
  config) mkdir -p config/package-lists ;;
  *) exit 0 ;;
esac
"""


@contextlib.contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Swallow (and hand back) everything the builder prints."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


class FakeEngineTestCase(unittest.TestCase):
    """Base class that pretends native live-build exists, backed by the stub binary."""

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("the stub live-build script needs a POSIX shell")
        if shutil.which("bash") is None:
            self.skipTest("bash is required for the stub live-build")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.write_fake_lb(FAKE_LB)
        self.patchers = [
            unittest.mock.patch.object(build_os, "native_build_ready", lambda: (True, "")),
            unittest.mock.patch.object(build_os, "docker_ready", lambda: (False, "stubbed away")),
            unittest.mock.patch.object(build_os, "free_gib", lambda path: 999.0),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = os.pathsep.join([str(self.bin), self.old_path])

    def tearDown(self) -> None:
        os.environ["PATH"] = self.old_path
        for patcher in self.patchers:
            patcher.stop()
        self.tmp.cleanup()

    def write_fake_lb(self, body: str) -> None:
        fake = self.bin / "lb"
        fake.write_text(body, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def build(self, *argv: str) -> tuple[int, str]:
        with quiet() as stream:
            code = build_os.main(list(argv))
        return code, stream.getvalue()


class NativePipelineTests(FakeEngineTestCase):
    def test_full_build_produces_iso_checksum_and_log(self) -> None:
        source = self.root / "motd"
        source.write_text("boot me\n", encoding="utf-8")
        hook = self.root / "customise.sh"
        hook.write_text("#!/bin/sh\necho hello from a hook\n", encoding="utf-8")
        output, workspace = self.root / "dist", self.root / "work"
        code, log = self.build(
            "--name",
            "Test OS",
            "--release",
            "bookworm",
            "--packages",
            "sudo,linux-image-amd64",
            "--copy",
            f"{source}:/etc/motd",
            "--hook",
            str(hook),
            "--output",
            str(output),
            "--work-dir",
            str(workspace),
        )
        self.assertEqual(code, 0, log)

        iso = output / "test-os-bookworm-amd64.iso"
        self.assertTrue(iso.is_file(), f"expected {iso} to exist; builder log:\n{log}")
        self.assertEqual(iso.read_bytes(), b"ISO9660 fake image data\n")

        checksum = Path(f"{iso}.sha256")
        self.assertTrue(checksum.is_file())
        digest, listed_name = checksum.read_text(encoding="utf-8").split()
        self.assertEqual(digest, hashlib.sha256(iso.read_bytes()).hexdigest())
        self.assertEqual(listed_name, iso.name)

        copied_log = Path(f"{iso}.log")
        self.assertTrue(copied_log.is_file())
        text = copied_log.read_text(encoding="utf-8")
        self.assertIn("lb config --distribution bookworm", text)
        self.assertIn("customisation applied", text)
        self.assertIn("fake lb: building an image", text)

        project = workspace / "live-os"
        package_list = project / "config" / "package-lists" / "os-maker.list.chroot"
        self.assertIn("sudo", package_list.read_text(encoding="utf-8"))
        self.assertIn("Generated by OS Maker", package_list.read_text(encoding="utf-8"))
        self.assertEqual(
            (project / "config" / "includes.chroot" / "etc" / "hostname").read_text(encoding="utf-8"),
            "test-os\n",
        )
        self.assertEqual(
            (project / "config" / "includes.chroot" / "etc" / "motd").read_text(encoding="utf-8"), "boot me\n"
        )
        applied_hook = project / "config" / "hooks" / "normal" / "0001-os-maker.hook.chroot"
        self.assertTrue(applied_hook.is_file())
        self.assertTrue(applied_hook.stat().st_mode & stat.S_IXUSR, "hooks must stay executable")
        self.assertIn(
            "--architectures amd64", (project / "config" / "last-config.txt").read_text(encoding="utf-8")
        )

    def test_output_matches_the_requested_architecture_and_release(self) -> None:
        output = self.root / "dist-arm"
        code, log = self.build(
            "--name", "Arm", "--architecture", "arm64", "--release", "trixie", "--output", str(output)
        )
        self.assertEqual(code, 0, log)
        # The ISO is renamed from the requested name, whatever live-build called it internally.
        self.assertEqual(
            sorted(path.name for path in output.iterdir()),
            ["arm-trixie-arm64.iso", "arm-trixie-arm64.iso.log", "arm-trixie-arm64.iso.sha256"],
        )

    def test_temporary_workspace_is_cleaned_up(self) -> None:
        output = self.root / "dist2"
        code, log = self.build("--name", "Temp", "--output", str(output))
        self.assertEqual(code, 0, log)
        self.assertTrue((output / "temp-bookworm-amd64.iso").is_file())
        leaked = list(Path(tempfile.gettempdir()).glob("os-maker-*"))
        self.assertEqual(leaked, [], "the scratch workspace should be removed")

    def test_keep_work_retains_the_workspace(self) -> None:
        workspace = self.root / "kept"
        code, log = self.build(
            "--name",
            "Kept",
            "--output",
            str(self.root / "dist3"),
            "--work-dir",
            str(workspace),
            "--keep-work",
        )
        self.assertEqual(code, 0, log)
        self.assertIn("workspace kept at", log)
        self.assertTrue((workspace / "live-os" / "os-maker" / "packages.txt").is_file())

    def test_build_failure_is_reported_with_the_exit_code_from_lb(self) -> None:
        os.environ["FAKE_LB_FAIL"] = "1"
        try:
            code, log = self.build("--name", "Broken", "--output", str(self.root / "dist4"))
        finally:
            del os.environ["FAKE_LB_FAIL"]
        self.assertEqual(code, 1)
        self.assertIn("fake lb: build blew up", log)
        self.assertIn("exit code 17", log)

    def test_stale_iso_is_never_shipped(self) -> None:
        workspace = self.root / "stale"
        project = workspace / "live-os"
        project.mkdir(parents=True)
        stale = project / "leftover.iso"
        stale.write_bytes(b"leftover from an earlier run")
        self.write_fake_lb(NO_OP_LB)
        code, log = self.build(
            "--name", "Stale", "--output", str(self.root / "dist5"), "--work-dir", str(workspace)
        )
        self.assertEqual(code, 1)
        self.assertIn("produced no ISO", log)
        self.assertFalse(stale.exists(), "a stale ISO must be deleted before building")

    def test_config_failure_stops_before_the_build(self) -> None:
        self.write_fake_lb("#!/usr/bin/env bash\nexit 9\n")
        output = self.root / "dist6"
        code, log = self.build("--name", "Nope", "--output", str(output))
        self.assertEqual(code, 1)
        self.assertIn("lb config failed", log)
        self.assertEqual(list(output.glob("*.iso")), [])

    def test_nothing_is_written_into_the_repository(self) -> None:
        repo = Path(build_os.__file__).parent
        before = {path.name for path in repo.iterdir()}
        self.build("--name", "Clean", "--output", str(self.root / "dist7"))
        self.assertEqual({path.name for path in repo.iterdir()}, before)


class DockerCommandTests(FakeEngineTestCase):
    """Check the command we hand to Docker by replacing ``run`` with a recorder."""

    def test_container_invocation_is_fully_assembled(self) -> None:
        (self.root / "motd").write_text("hello\n", encoding="utf-8")
        recorded: list[list[str]] = []

        def recorder(command, **kwargs: object) -> build_os.CommandResult:
            recorded.append([str(part) for part in command])
            project = Path(kwargs["cwd"]) / "live-os"
            project.mkdir(parents=True, exist_ok=True)
            (project / "docker-image.iso").write_bytes(b"fake")
            return build_os.CommandResult(0)

        with (
            unittest.mock.patch.object(build_os, "native_build_ready", lambda: (False, "no native tools")),
            unittest.mock.patch.object(build_os, "docker_ready", lambda: (True, "27.0.0")),
            unittest.mock.patch.object(build_os, "run", recorder),
            quiet() as stream,
        ):
            code = build_os.main(
                [
                    "--name",
                    "Container",
                    "--container",
                    "always",
                    "--copy",
                    f"{self.root / 'motd'}:/etc/motd",
                    "--output",
                    str(self.root / "dist"),
                    "--work-dir",
                    str(self.root / "work"),
                ]
            )
        self.assertEqual(code, 0, stream.getvalue())
        self.assertEqual(len(recorded), 1, "exactly one docker run call is expected")
        docker = recorded[0]
        self.assertEqual(docker[:2], ["docker", "run"])
        self.assertIn("--privileged", docker)
        self.assertIn("-v", docker)
        expected_workspace = (self.root / "work").resolve().as_posix()  # main() resolves the path
        self.assertIn(f"{expected_workspace}:/work", docker)
        self.assertEqual(docker[docker.index("-w") + 1], "/work/live-os")
        self.assertEqual(docker[docker.index("-w") + 2], "debian:bookworm-slim")
        script = docker[-1]
        for step in (
            "apt-get update",
            "apt-get install -y live-build",
            "lb config",
            "apply-staging.sh",
            "lb build",
        ):
            self.assertIn(step, script)
        self.assertEqual(sorted((self.root / "dist").glob("*.iso"))[0].name, "container-bookworm-amd64.iso")

    def test_cross_architecture_build_requests_a_platform(self) -> None:
        """Building arm64 on amd64 (or the reverse) must pin the container platform."""
        recorded: list[list[str]] = []

        def recorder(command, **kwargs: object) -> build_os.CommandResult:
            recorded.append([str(part) for part in command])
            project = Path(kwargs["cwd"]) / "live-os"
            project.mkdir(parents=True, exist_ok=True)
            (project / "cross.iso").write_bytes(b"fake")
            return build_os.CommandResult(0)

        target = "arm64" if build_os.host_architecture() != "arm64" else "amd64"
        with (
            unittest.mock.patch.object(build_os, "native_build_ready", lambda: (False, "")),
            unittest.mock.patch.object(build_os, "docker_ready", lambda: (True, "1")),
            unittest.mock.patch.object(build_os, "run", recorder),
            quiet() as stream,
        ):
            code = build_os.main(
                [
                    "--name",
                    "Cross",
                    "--container",
                    "always",
                    "--architecture",
                    target,
                    "--output",
                    str(self.root / "cross-dist"),
                ]
            )
        self.assertEqual(code, 0, stream.getvalue())
        joined = " ".join(recorded[0])
        if build_os.host_architecture() in (None, target):
            self.assertNotIn("--platform", joined)
        else:
            self.assertIn(f"--platform linux/{target}", joined)

    def test_cache_dir_is_mounted_into_the_container(self) -> None:
        cache = self.root / "apt-cache"
        command = build_os.docker_run_command(
            workspace=self.root, build_image="debian:trixie-slim", script="lb build", cache_dir=cache
        )
        self.assertIn(f"{cache.as_posix()}:/var/cache/apt/archives", command)
        self.assertIn(f"{(cache / 'partial').as_posix()}:/var/cache/apt/archives/partial", command)

    def test_docker_daemon_problems_are_surfaced(self) -> None:
        # docker_ready() itself: no daemon here should not raise, it must return a reason.
        usable, detail = build_os.docker_ready()
        self.assertIsInstance(usable, bool)
        if not usable:
            self.assertTrue(detail, "a reason is required when Docker is unusable")


class BootTestTests(FakeEngineTestCase):
    def test_boot_test_uses_a_stub_qemu(self) -> None:
        fake = self.bin / "qemu-system-x86_64"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "# fake qemu: find -serial file:PATH and pretend the live system booted\n"
            'for arg in "$@"; do case "$arg" in file:*) echo "Debian GNU/Linux 12 osmaker ttyS0" > "${arg#file:}" ;; esac; done\n'
            "sleep 30\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        iso = self.root / "image.iso"
        iso.write_bytes(b"fake")
        with quiet() as stream:
            code = build_os.qemu_boot_test(iso, "amd64", 30)
        self.assertEqual(code, 0, stream.getvalue())
        self.assertIn("Boot test passed", stream.getvalue())

    def test_boot_test_fails_when_the_image_is_silent(self) -> None:
        fake = self.bin / "qemu-system-x86_64"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        iso = self.root / "image.iso"
        iso.write_bytes(b"fake")
        with quiet(), self.assertRaises(build_os.BuildError) as caught:
            build_os.qemu_boot_test(iso, "amd64", 2)
        self.assertIn("boot test failed", str(caught.exception))

    def test_boot_test_is_skipped_for_arm64(self) -> None:
        with quiet() as stream:
            code = build_os.qemu_boot_test(self.root / "image.iso", "arm64", 5)
        self.assertEqual(code, 0)
        self.assertIn("only supports amd64", stream.getvalue())

    def test_missing_qemu_is_reported(self) -> None:
        os.environ["PATH"] = str(self.bin)  # only the fake lb lives here
        try:
            with quiet(), self.assertRaises(build_os.BuildError) as caught:
                build_os.qemu_boot_test(self.root / "image.iso", "amd64", 5)
        finally:
            os.environ["PATH"] = self.old_path
        self.assertIn("qemu-system-x86_64 was not found", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
