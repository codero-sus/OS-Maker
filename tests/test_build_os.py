"""Unit tests for the ISO builder (:mod:`build_os`).

These never invoke Docker, QEMU or ``lb``: they cover the pure decision helpers,
the staging tree that is handed to live-build, and the command assembly.

Run from the repository root::

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shlex
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_os


@contextlib.contextmanager
def captured() -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """Capture stdout and stderr; ``text`` is both of them combined."""

    class Captured:
        def __init__(self, out: io.StringIO, err: io.StringIO) -> None:
            self.out, self.err = out, err

        def getvalue(self) -> str:
            return self.out.getvalue() + self.err.getvalue()

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield Captured(out, err)


class PackageTests(unittest.TestCase):
    def test_default_kernel_matches_the_architecture(self) -> None:
        for architecture in build_os.ARCHITECTURES:
            with self.subTest(architecture=architecture):
                packages = build_os.default_packages(architecture)
                self.assertIn(f"linux-image-{architecture}", packages)
                self.assertIn("live-boot", packages)

    def test_arm64_build_does_not_borrow_the_amd64_kernel(self) -> None:
        """Regression: an arm64 ISO cannot boot an amd64 kernel."""
        self.assertNotIn("linux-image-amd64", build_os.default_packages("arm64"))

    def test_packages_are_deduplicated_and_trimmed(self) -> None:
        packages = build_os.parse_packages(" sudo , vim ,sudo,,linux-image-amd64", "amd64")
        self.assertEqual(packages[:3], ["sudo", "vim", "linux-image-amd64"])
        self.assertEqual(packages.count("sudo"), 1)
        self.assertTrue(all(package == package.strip() for package in packages))

    def test_a_kernel_is_added_when_missing(self) -> None:
        with captured():
            packages = build_os.parse_packages("sudo,htop", "arm64")
        self.assertEqual(packages[0], "linux-image-arm64")
        self.assertIn("sudo", packages)

    def test_mismatched_kernel_is_rejected(self) -> None:
        with self.assertRaises(build_os.BuildError) as caught:
            build_os.parse_packages("linux-image-amd64,sudo", "arm64")
        self.assertIn("cannot boot", str(caught.exception))

    def test_shell_metacharacters_are_rejected(self) -> None:
        for raw in ("sudo; rm -rf /", "a b", "--evil", "$(id)", ""):
            with self.subTest(raw=raw), self.assertRaises(build_os.BuildError):
                build_os.parse_packages(raw, "amd64")

    def test_architecture_qualified_names_are_allowed(self) -> None:
        self.assertIn("vim:amd64", build_os.parse_packages("vim:amd64,linux-image-amd64", "amd64"))


class NameTests(unittest.TestCase):
    def test_hostname_is_ascii_safe(self) -> None:
        cases = {
            "My OS": "my-os",
            "Ångström Linux!": "ngstr-m-linux",
            "  ": "myos",
            "StudyOS": "studyos",
            "---": "myos",
            "a..b": "a-b",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(build_os.sanitize_hostname(raw), expected)

    def test_hostname_respects_the_63_character_limit(self) -> None:
        slug = build_os.sanitize_hostname("x" * 200)
        self.assertLessEqual(len(slug), 63)
        self.assertTrue(slug)

    def test_generated_names_are_valid_hostnames(self) -> None:
        for raw in ("My OS", "Ünïcødé", "1234", "x" * 100):
            slug = build_os.sanitize_hostname(raw)
            self.assertRegex(slug, r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

    def test_copy_spec_parsing(self) -> None:
        source, destination = build_os.parse_copy_spec("./motd:/etc/motd")
        self.assertEqual((source, destination), ("./motd", PurePosixPath("/etc/motd")))
        source, destination = build_os.parse_copy_spec(
            r"C:\Users\me\wall.png:/usr/share/backgrounds/wall.png"
        )
        self.assertEqual(source, r"C:\Users\me\wall.png")
        self.assertEqual(str(destination), "/usr/share/backgrounds/wall.png")

    def test_copy_spec_rejects_dangerous_or_incomplete_destinations(self) -> None:
        for spec in ("onlyone", ":/etc/x", "src:", "src:relative/path", "src:/", "src:/etc/../etc/x"):
            with self.subTest(spec=spec), self.assertRaises(build_os.BuildError):
                build_os.parse_copy_spec(spec)


class CommandAssemblyTests(unittest.TestCase):
    def test_lb_config_carries_the_important_settings(self) -> None:
        command = build_os.lb_config_command(release="trixie", architecture="amd64", name="Study OS")
        for expected in (
            "--distribution",
            "trixie",
            "--architectures",
            "amd64",
            "--binary-images",
            "iso-hybrid",
        ):
            self.assertIn(expected, command)
        self.assertEqual(command[command.index("--iso-application") + 1], "Study OS")

    def test_firmware_and_compression_flags(self) -> None:
        plain = build_os.lb_config_command(release="bookworm", architecture="amd64", name="X")
        self.assertNotIn("--archive-areas", plain)
        self.assertNotIn("--compression", plain)
        tuned = build_os.lb_config_command(
            release="bookworm", architecture="amd64", name="X", compression="zstd", with_firmware=True
        )
        self.assertEqual(tuned[tuned.index("--compression") + 1], "zstd")
        self.assertIn("non-free-firmware", " ".join(tuned))

    def test_iso_volume_id_fits_the_limit(self) -> None:
        command = build_os.lb_config_command(release="bookworm", architecture="amd64", name="n" * 80)
        self.assertLessEqual(len(command[command.index("--iso-volume") + 1]), 32)

    def test_build_image_tracks_the_release(self) -> None:
        self.assertEqual(build_os.build_image_for("bookworm", None), "debian:bookworm-slim")
        self.assertEqual(build_os.build_image_for("trixie", None), "debian:trixie-slim")
        self.assertEqual(build_os.build_image_for("sid", None), "debian:trixie-slim")
        self.assertEqual(build_os.build_image_for("bookworm", "debian:unstable"), "debian:unstable")

    def test_container_script_runs_the_stages_in_order(self) -> None:
        config = build_os.lb_config_command(release="bookworm", architecture="amd64", name="My OS")
        script = build_os.docker_setup_script(config)
        steps = [line.split()[0] for line in script.splitlines()]
        self.assertEqual(steps, ["set", "export", "apt-get", "apt-get", "lb", "bash", "lb"])
        self.assertIn("live-build", script)
        self.assertIn("apply-staging.sh", script)
        self.assertNotIn("--no-install-recommends", script)  # the ISO tools arrive as recommends

    def test_container_script_survives_a_wild_display_name(self) -> None:
        """The name is user input that ends up inside a shell script: it must be quoted."""
        name = "My; rm -rf / OS `id` $(id)"
        config = build_os.lb_config_command(release="bookworm", architecture="amd64", name=name)
        script = build_os.docker_setup_script(config)
        lb_line = next(line for line in script.splitlines() if line.startswith("lb config"))
        self.assertEqual(shlex.split(lb_line), config)  # bash sees the same argv we do

    def test_container_workspace_paths_are_mounted_verbatim(self) -> None:
        # Spaces in a workspace path must not break the bind mount (shlex round-trip again).
        command = build_os.docker_run_command(
            workspace=Path("/tmp/my work/live"), build_image="debian:bookworm-slim", script="lb build"
        )
        self.assertIn("/tmp/my work/live:/work", command)

    def test_docker_run_mounts_and_platform(self) -> None:
        command = build_os.docker_run_command(
            workspace=Path("/tmp/os-work"),
            build_image="debian:bookworm-slim",
            script="lb build",
            host_architecture="amd64",
            target_architecture="arm64",
            cache_dir=Path("/tmp/apt-cache"),
        )
        joined = " ".join(command)
        self.assertIn("--privileged", joined)
        self.assertIn("--platform linux/arm64", joined)
        self.assertIn("/tmp/os-work:/work", joined)
        self.assertIn("/tmp/apt-cache:/var/cache/apt/archives", joined)
        self.assertEqual(command[command.index("-w") + 1], "/work/live-os")
        self.assertEqual(command[-3:], ["bash", "-lc", "lb build"])

    def test_apt_cache_mount_includes_the_partial_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "apt"
            command = build_os.docker_run_command(
                workspace=Path(tmp), build_image="debian:bookworm-slim", script="lb build", cache_dir=cache
            )
            mounts = [command[i + 1] for i, part in enumerate(command) if part == "-v"]
            self.assertEqual(
                mounts,
                [
                    f"{Path(tmp).as_posix()}:/work",
                    f"{cache.as_posix()}:/var/cache/apt/archives",
                    f"{(cache / 'partial').as_posix()}:/var/cache/apt/archives/partial",
                ],
            )
            self.assertTrue((cache / "partial").is_dir(), "apt requires the partial directory to exist")

    def test_docker_run_avoids_the_platform_flag_on_matching_arch(self) -> None:
        command = build_os.docker_run_command(
            workspace=Path("/tmp/w"),
            build_image="debian:bookworm-slim",
            script="x",
            host_architecture="amd64",
            target_architecture="amd64",
        )
        self.assertNotIn("--platform", command)
        self.assertNotIn("/var/cache/apt/archives", " ".join(command))

    @unittest.skipIf(platform.system() != "Windows", "Windows path separators are the interesting case")
    def test_windows_workspace_is_mounted_with_forward_slashes(self) -> None:
        command = build_os.docker_run_command(
            workspace=Path("C:/Users/me/build"),
            build_image="i",
            script="x",
            host_architecture="amd64",
            target_architecture="amd64",
        )
        self.assertIn("C:/Users/me/build:/work", " ".join(command))

    def test_staging_script_is_valid_bash(self) -> None:
        bash = shutil.which("bash")
        if bash is None:  # pragma: no cover
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "live-os"
            (project / build_os.STAGING_DIRNAME).mkdir(parents=True)
            script = project / build_os.STAGING_DIRNAME / build_os.STAGING_SCRIPT_NAME
            script.write_text(build_os.STAGING_SCRIPT, encoding="utf-8")
            checked = subprocess_run([bash, "-n", str(script)])
            self.assertEqual(checked, 0, "apply-staging.sh must parse")

    def test_staging_applies_customisation(self) -> None:
        bash = shutil.which("bash")
        if bash is None:  # pragma: no cover
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "live-os"
            project.mkdir()
            source = root / "motd"
            source.write_text("hello from the host\n", encoding="utf-8")
            directory = root / "extra"
            (directory / "sub").mkdir(parents=True)
            (directory / "sub" / "file").write_text("nested\n", encoding="utf-8")
            hook = root / "customise.sh"
            hook.write_text("#!/bin/sh\necho running a hook\n", encoding="utf-8")
            with captured():
                notes = build_os.stage_project(
                    project,
                    packages=["linux-image-amd64", "sudo"],
                    hostname="studyos",
                    copies=[
                        (str(source), PurePosixPath("/etc/motd")),
                        (str(directory), PurePosixPath("/opt/extra")),
                    ],
                    hooks=[hook],
                )
            self.assertEqual(len(notes), 5)  # packages, hostname, 1 hook, 2 copies
            staging = project / build_os.STAGING_DIRNAME
            self.assertEqual(
                (staging / "packages.txt").read_text(encoding="utf-8"), "linux-image-amd64\nsudo\n"
            )
            self.assertEqual((staging / "hostname").read_text(encoding="utf-8"), "studyos\n")
            self.assertEqual(
                (staging / "includes.chroot" / "etc" / "motd").read_text(encoding="utf-8"),
                "hello from the host\n",
            )
            self.assertEqual(
                (staging / "includes.chroot" / "opt" / "extra" / "sub" / "file").read_text(encoding="utf-8"),
                "nested\n",
            )
            hook_copy = staging / "hooks" / "0001-os-maker.hook.chroot"
            self.assertTrue(hook_copy.is_file())
            # live-build only runs executable hooks, and host modes do not survive a
            # Windows/macOS bind mount, so apply-staging.sh re-marks them in the container.
            self.assertIn("chmod +x config/hooks/normal/*.hook.chroot", build_os.STAGING_SCRIPT)
            if os.name != "nt":
                self.assertTrue(os.access(hook_copy, os.X_OK), "hooks are executable on POSIX hosts")

            # emulate 'lb config' creating the config tree, then let the script do its job
            (project / "config").mkdir()
            run_code = subprocess_run(
                [bash, f"{build_os.STAGING_DIRNAME}/{build_os.STAGING_SCRIPT_NAME}"], cwd=project
            )
            self.assertEqual(run_code, 0)
            package_list = project / "config" / "package-lists" / "os-maker.list.chroot"
            self.assertIn("linux-image-amd64", package_list.read_text(encoding="utf-8"))
            self.assertEqual(
                (project / "config" / "includes.chroot" / "etc" / "hostname").read_text(encoding="utf-8"),
                "studyos\n",
            )
            if os.name != "nt":  # NTFS has no mode bits; the in-container chmod is what matters
                self.assertTrue(
                    (project / "config" / "hooks" / "normal" / "0001-os-maker.hook.chroot").stat().st_mode
                    & 0o111
                )
            self.assertEqual(
                (project / "config" / "includes.chroot" / "etc" / "motd").read_text(encoding="utf-8"),
                "hello from the host\n",
            )

    def test_missing_copy_source_or_hook_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "live-os"
            project.mkdir()
            with self.assertRaises(build_os.BuildError), captured():
                build_os.stage_project(
                    project,
                    packages=["x"],
                    hostname="h",
                    copies=[("/nope/such/file", PurePosixPath("/etc/x"))],
                    hooks=[],
                )
            with self.assertRaises(build_os.BuildError), captured():
                build_os.stage_project(
                    project, packages=["x"], hostname="h", copies=[], hooks=[Path(tmp) / "missing.sh"]
                )


class IsoDiscoveryTests(unittest.TestCase):
    def test_newest_iso_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            old = project / "old.iso"
            old.write_bytes(b"old")
            os.utime(old, (time.time() - 3600, time.time() - 3600))
            new = project / "new.iso"
            new.write_bytes(b"new")
            self.assertEqual(build_os.find_iso(project).name, "new.iso")

    def test_stale_iso_is_not_mistaken_for_a_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "previous.iso"
            stale.write_bytes(b"old")
            os.utime(stale, (time.time() - 7200, time.time() - 7200))
            with self.assertRaises(build_os.BuildError) as caught:
                build_os.find_iso(Path(tmp), not_older_than=time.time())
            self.assertIn("earlier build", str(caught.exception))
            self.assertEqual(
                build_os.find_iso(Path(tmp)).name, "previous.iso"
            )  # without the filter it is found

    def test_no_iso_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build_os.BuildError):
            build_os.find_iso(Path(tmp))


class HelperTests(unittest.TestCase):
    def test_sha256_matches_hashlib(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            path.write_bytes(b"1234567890" * 1000)
            self.assertEqual(
                build_os.sha256_of(path, chunk_size=64), hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_select_build_mode_truth_table(self) -> None:
        table = [
            (("auto", True, True), "native"),
            (("auto", False, True), "docker"),
            (("auto", True, False), "native"),
            (("always", True, True), "docker"),
            (("always", True, False), "error"),
            (("never", False, True), "error"),
            (("never", True, False), "native"),
            (("auto", False, False), "error"),
        ]
        for (requested, native, docker), expected in table:
            with self.subTest(requested=requested, native=native, docker=docker):
                mode, reason = build_os.select_build_mode(requested, native, docker)
                self.assertEqual(mode, expected)
                self.assertTrue(bool(reason) == (expected == "error"))

    def test_free_space_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            free = build_os.free_gib(Path(tmp))
            self.assertIsNotNone(free)
            assert free is not None
            self.assertGreaterEqual(free, 0)
        self.assertIsNone(build_os.free_gib(Path("/definitely/not/here")))

    def test_host_architecture_is_a_debian_arch(self) -> None:
        detected = build_os.host_architecture()
        self.assertTrue(detected is None or detected in build_os.ARCHITECTURES)

    def test_run_streams_output_and_writes_a_log(self) -> None:
        bash = shutil.which("bash")
        if bash is None:  # pragma: no cover
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "build.log"
            with captured() as out:
                result = build_os.run([bash, "-c", "echo one; echo two >&2; exit 4"], log_path=log)
            self.assertEqual(result.returncode, 4)
            self.assertFalse(result.ok)
            self.assertIn("one", out.getvalue())
            self.assertIn("two", out.getvalue())
            text = log.read_text(encoding="utf-8")
            self.assertIn("$ ", text)
            self.assertIn("two", text)

    def test_run_records_the_exit_code_without_raising(self) -> None:
        with captured():
            result = build_os.run([sys.executable, "-c", "print('x')"])
        self.assertTrue(result.ok)
        self.assertEqual(result.tail[-1], "x")

    def test_log_tail_is_bounded(self) -> None:
        bash = shutil.which("bash")
        if bash is None:  # pragma: no cover
            self.skipTest("bash is not available")
        with captured():
            result = build_os.run([bash, "-c", "for i in $(seq 1 200); do echo line$i; done"])
        self.assertLessEqual(len(result.tail), 41)
        self.assertEqual(result.tail[-1], "line200")


class CommandLineTests(unittest.TestCase):
    def main(self, argv: list[str]) -> tuple[int, str]:
        with captured() as out:
            code = build_os.main(argv)
        return code, out.getvalue()

    def test_version_and_help_are_available(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_os.build_parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        text = build_os.build_parser().format_help()
        for flag in ("--copy", "--hook", "--compression", "--boot-test", "--dry-run", "--cache-dir"):
            self.assertIn(flag, text)

    def test_invalid_input_is_rejected_before_any_build(self) -> None:
        for argv in (
            ["--name", "  "],
            ["--name", "line1\nline2"],
            ["--release", "Bad Release"],
            ["--boot-timeout", "0"],
            ["--packages", "not,a!package"],
            ["--copy", "nocolon"],
        ):
            with self.subTest(argv=argv):
                code, output = self.main([*argv, "--dry-run"])
                self.assertEqual(code, 2)
                self.assertTrue(output.strip())

    def test_dry_run_prints_a_plan_without_touching_the_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            code, text = self.main(
                [
                    "--name",
                    "Plan Only",
                    "--release",
                    "trixie",
                    "--architecture",
                    "arm64",
                    "--packages",
                    "sudo,htop",
                    "--compression",
                    "lz4",
                    "--output",
                    str(output_dir),
                    "--dry-run",
                ]
            )
            self.assertEqual(code, 0, text)
            self.assertFalse(output_dir.exists(), "a dry run must not create the output directory")
            self.assertIn("plan-only-trixie-arm64.iso", text)
            self.assertIn("linux-image-arm64", text)
            self.assertIn("--compression lz4", text.replace("\\", ""))
            self.assertTrue("docker:" in text or "commands:" in text, text)

    def test_dry_run_reports_copies_hooks_and_the_boot_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "motd"
            source.write_text("hi\n", encoding="utf-8")
            hook = Path(tmp) / "hook.sh"
            hook.write_text("#!/bin/sh\n", encoding="utf-8")
            code, text = self.main(
                [
                    "--name",
                    "Planned",
                    "--copy",
                    f"{source}:/etc/motd",
                    "--hook",
                    str(hook),
                    "--boot-test",
                    "--dry-run",
                ]
            )
            self.assertEqual(code, 0, text)
            self.assertIn("/etc/motd", text)
            self.assertIn("hook.sh", text)
            self.assertIn("boot test", text)

    def test_unusable_engine_stops_a_real_build(self) -> None:
        if shutil.which("docker") and shutil.which("lb") and getattr(os, "geteuid", lambda: 1)() == 0:
            self.skipTest("this host can actually build, so the failure path does not apply")
        code, text = self.main(["--name", "Blocked", "--container", "never"])
        self.assertEqual(code, 2)
        self.assertIn("native live-build", text)

    def test_unknown_release_only_warns(self) -> None:
        code, _ = self.main(["--name", "Sid", "--release", "sarge", "--dry-run"])
        self.assertEqual(code, 0)


class EngineProbeTests(unittest.TestCase):
    """How the builder decides *where* to run, on a simulated host."""

    @staticmethod
    def args(argv: list[str]) -> object:
        return build_os.build_parser().parse_args(argv)

    def test_native_build_is_refused_off_linux(self) -> None:
        with unittest.mock.patch.object(build_os.platform, "system", return_value="Darwin"):
            usable, reason = build_os.native_build_ready()
        self.assertFalse(usable)
        self.assertIn("only runs on Linux", reason)

    def test_native_build_lists_the_tools_it_misses(self) -> None:
        installed: dict[str, str | None] = {}
        with (
            unittest.mock.patch.object(build_os.platform, "system", return_value="Linux"),
            unittest.mock.patch.object(
                build_os.shutil, "which", side_effect=lambda tool: installed.get(tool)
            ),
        ):
            usable, reason = build_os.native_build_ready()
            self.assertFalse(usable)
            self.assertIn("live-build is not installed", reason)

            installed["lb"] = "/usr/bin/lb"  # with lb present, the tool check itself has the say
            usable, reason = build_os.native_build_ready()
            self.assertFalse(usable)
            self.assertIn("missing build tools: debootstrap, xorriso, mksquashfs", reason)
            self.assertIn("apt-get install", reason)  # and it says how to fix it

            installed["mksquashfs"] = "/usr/bin/mksquashfs"
            _, reason = build_os.native_build_ready()
            self.assertIn("debootstrap, xorriso", reason)
            self.assertNotIn("mksquashfs:", reason)

    def test_native_build_needs_root(self) -> None:
        with (
            unittest.mock.patch.object(build_os.platform, "system", return_value="Linux"),
            unittest.mock.patch.object(build_os.shutil, "which", return_value="/usr/bin/tool"),
            unittest.mock.patch.object(build_os.os, "geteuid", return_value=1000, create=True),
        ):
            usable, reason = build_os.native_build_ready()
        self.assertFalse(usable)
        self.assertIn("as root", reason)

    def test_native_build_is_ready_when_all_is_well(self) -> None:
        with (
            unittest.mock.patch.object(build_os.platform, "system", return_value="Linux"),
            unittest.mock.patch.object(build_os.shutil, "which", return_value="/usr/bin/tool"),
            unittest.mock.patch.object(build_os.os, "geteuid", return_value=0, create=True),
        ):
            self.assertEqual(build_os.native_build_ready(), (True, ""))

    def test_docker_is_unusable_without_the_binary(self) -> None:
        with unittest.mock.patch.object(build_os.shutil, "which", return_value=None):
            usable, reason = build_os.docker_ready()
        self.assertFalse(usable)
        self.assertIn("not found on PATH", reason)

    def test_docker_ready_reports_the_daemon_version(self) -> None:
        completed = unittest.mock.Mock(returncode=0, stdout="27.1.1\n", stderr="")
        with (
            unittest.mock.patch.object(build_os.shutil, "which", return_value="/usr/bin/docker"),
            unittest.mock.patch.object(build_os.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(build_os.docker_ready(), (True, "27.1.1"))

    def test_docker_ready_reports_a_stopped_daemon(self) -> None:
        completed = unittest.mock.Mock(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        )
        with (
            unittest.mock.patch.object(build_os.shutil, "which", return_value="/usr/bin/docker"),
            unittest.mock.patch.object(build_os.subprocess, "run", return_value=completed),
        ):
            usable, reason = build_os.docker_ready()
        self.assertFalse(usable)
        self.assertIn("daemon is not reachable", reason)
        self.assertIn("Cannot connect", reason)

    def test_docker_ready_reports_a_broken_install(self) -> None:
        with (
            unittest.mock.patch.object(build_os.shutil, "which", return_value="/usr/bin/docker"),
            unittest.mock.patch.object(build_os.subprocess, "run", side_effect=OSError("exec format error")),
        ):
            usable, reason = build_os.docker_ready()
        self.assertFalse(usable)
        self.assertIn("could not run 'docker info'", reason)

    def test_select_engine_prefers_native_and_stays_quiet_about_the_daemon(self) -> None:
        with (
            unittest.mock.patch.object(build_os, "native_build_ready", return_value=(True, "")),
            unittest.mock.patch.object(build_os, "docker_ready", return_value=(True, "27.1.1")),
        ):
            self.assertEqual(
                build_os.select_engine(self.args(["--name", "X", "--container", "auto"])), ("native", None)
            )

    def test_select_engine_reports_the_daemon_it_found(self) -> None:
        with (
            unittest.mock.patch.object(build_os, "native_build_ready", return_value=(False, "not root")),
            unittest.mock.patch.object(build_os, "docker_ready", return_value=(True, "27.1.1")),
        ):
            self.assertEqual(build_os.select_engine(self.args(["--name", "X"])), ("docker", "27.1.1"))

    def test_select_engine_explains_every_option(self) -> None:
        with (
            unittest.mock.patch.object(
                build_os, "native_build_ready", return_value=(False, "live-build missing")
            ),
            unittest.mock.patch.object(build_os, "docker_ready", return_value=(False, "docker missing")),
            self.assertRaises(build_os.BuildError) as caught,
        ):
            build_os.select_engine(self.args(["--name", "X"]))
        message = str(caught.exception)
        for line in (
            "live-build missing",
            "native live-build: live-build missing",
            "docker missing",
            "hint:",
        ):
            self.assertIn(line, message)

    def test_select_engine_drops_the_hint_when_the_user_ruled_docker_out(self) -> None:
        with (
            unittest.mock.patch.object(build_os, "native_build_ready", return_value=(False, "not root")),
            unittest.mock.patch.object(build_os, "docker_ready", return_value=(False, "docker missing")),
            self.assertRaises(build_os.BuildError) as caught,
        ):
            build_os.select_engine(self.args(["--name", "X", "--container", "never"]))
        self.assertNotIn("hint:", str(caught.exception))

    def test_a_dry_run_plans_a_docker_build_even_without_an_engine(self) -> None:
        with (
            unittest.mock.patch.object(build_os, "native_build_ready", return_value=(False, "no")),
            unittest.mock.patch.object(build_os, "docker_ready", return_value=(False, "nope")),
            captured() as out,
        ):
            mode, daemon = build_os.select_engine(self.args(["--name", "X", "--dry-run"]))
        self.assertEqual((mode, daemon), ("docker", None))
        self.assertIn("showing the plan anyway", out.getvalue())


class CommandRunTests(unittest.TestCase):
    def test_run_streams_logs_and_keeps_the_tail(self) -> None:
        script = "for i in range(1, 46): print('line%d' % i)\nprint('oops')\nraise SystemExit(3)"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "build.log"
            with captured():
                result = build_os.run([sys.executable, "-c", script], log_path=log)
            written = log.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.tail), 40)  # only the last 40 lines are kept for the error report
        self.assertEqual(result.tail[0], "line7")  # 46 lines in, the last 40 kept
        self.assertEqual(result.tail[-1], "oops")  # stderr is merged into the same stream
        self.assertIn("$ ", written)
        self.assertIn("line45", written)

    def test_run_survives_a_process_without_a_pipe(self) -> None:
        class Silent:
            stdout = None

            def wait(self) -> int:
                return 0

        with unittest.mock.patch.object(build_os.subprocess, "Popen", return_value=Silent()):
            result = build_os.run(["true"], echo=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.tail, [])

    def test_quiet_run_hides_the_command_but_not_the_progress(self) -> None:
        with captured() as out:
            build_os.run([sys.executable, "-c", "print('hi')"], echo=False)
        text = out.getvalue()
        self.assertNotIn("$ ", text)
        self.assertIn("hi", text)


class BootTestTests(unittest.TestCase):
    """--boot-test: the QEMU smoke test, with QEMU standing in for a fake process."""

    class FakeQEMU:
        """A child process that "runs" by writing a console log, then exits."""

        def __init__(self, command: list[str], text: str, *, exit_code: int = 0) -> None:
            self.log = Path(command[command.index("-serial") + 1][len("file:") :])
            self.exit_code = exit_code
            self.terminated = False
            if text:  # QEMU writes the serial log as it boots, before anyone polls
                self.log.write_text(text, encoding="utf-8")

        def poll(self) -> int | None:
            return self.exit_code if self.log.exists() or self.exit_code else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float = 0) -> int:
            return self.exit_code

        def kill(self) -> None:
            return None

    @staticmethod
    def fake_popen(text: str, exit_code: int = 0):
        def spawn(command, **_kwargs):
            return BootTestTests.FakeQEMU(list(command), text, exit_code=exit_code)

        return unittest.mock.patch.object(build_os.subprocess, "Popen", side_effect=spawn)

    def test_wait_for_boot_returns_the_console_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            log.write_text("Debian GNU/Linux 13 live-testing login: ", encoding="utf-8")
            text = build_os.wait_for_boot(log, unittest.mock.Mock(poll=lambda: None), timeout=2)
        self.assertIn("login:", text or "")

    def test_wait_for_boot_gives_up_when_qemu_exits_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = unittest.mock.Mock(poll=lambda: 1)
            self.assertIsNone(build_os.wait_for_boot(Path(tmp) / "missing.log", process, timeout=2))

    def test_boot_test_is_skipped_for_arm_images(self) -> None:
        with captured() as out:
            self.assertEqual(build_os.qemu_boot_test(Path("x.iso"), "arm64", 5), 0)
        self.assertIn("only supports amd64", out.getvalue())

    def test_boot_test_needs_qemu_installed(self) -> None:
        with (
            unittest.mock.patch.object(build_os.shutil, "which", return_value=None),
            self.assertRaises(build_os.BuildError) as caught,
        ):
            build_os.qemu_boot_test(Path("x.iso"), "amd64", 5)
        self.assertIn("qemu-system-x86_64 was not found", str(caught.exception))

    def test_boot_test_passes_when_the_login_prompt_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso, log = Path(tmp) / "x.iso", Path(tmp) / "build.log"
            iso.write_bytes(b"ISO9660")
            log.write_text("earlier output\n", encoding="utf-8")
            with (
                unittest.mock.patch.object(
                    build_os.shutil, "which", return_value="/usr/bin/qemu-system-x86_64"
                ),
                self.fake_popen("Debian GNU/Linux 13\nlive-testing login: "),
                captured() as out,
            ):
                code = build_os.qemu_boot_test(iso, "amd64", 5, log_path=log)
            self.assertEqual(code, 0)
            self.assertIn("Boot test passed", out.getvalue())
            self.assertIn("live-testing login", log.read_text(encoding="utf-8"))

    def test_boot_test_fails_on_a_blank_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "x.iso"
            iso.write_bytes(b"ISO9660")
            with (
                unittest.mock.patch.object(
                    build_os.shutil, "which", return_value="/usr/bin/qemu-system-x86_64"
                ),
                self.fake_popen("", exit_code=1),
                captured() as out,
                self.assertRaises(build_os.BuildError) as caught,
            ):
                build_os.qemu_boot_test(iso, "amd64", 5)
        self.assertIn("no live-system banner", str(caught.exception))
        self.assertIn("no console output", out.getvalue())


class PlanTests(unittest.TestCase):
    """decide()/Workspace/report: the phases main() used to do inline."""

    @staticmethod
    def plan(argv: list[str]) -> build_os.Plan:
        """decide() as if Docker were available, so plans can be built anywhere."""
        with unittest.mock.patch.object(build_os, "select_engine", return_value=("docker", None)):
            return build_os.decide(build_os.build_parser().parse_args(argv))

    def test_bad_flags_are_rejected_before_any_work(self) -> None:
        cases = [
            (["--name", " "], "non-empty"),
            (["--name", "a\nb"], "non-empty"),
            (["--name", "Ok", "--release", "Two Words"], "invalid --release"),
            (["--name", "Ok", "--boot-timeout", "0"], "positive number"),
            (["--name", "Ok", "--cache-dir", "/tmp/c", "--container", "never"], "only applies to Docker"),
            (["--name", "Ok", "--packages", "!!"], "invalid package name"),
            (["--name", "Ok", "--copy", "nocolon"], "SOURCE:DESTINATION"),
        ]
        for argv, fragment in cases:
            with self.subTest(argv=argv), self.assertRaises(build_os.BuildError) as caught:
                self.plan(argv)
            self.assertIn(fragment, str(caught.exception))

    def test_a_plan_names_the_image_after_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(
                ["--name", "My Desk OS", "--release", "trixie", "--architecture", "arm64", "--output", tmp]
            )
        self.assertEqual(plan.hostname, "my-desk-os")
        self.assertEqual(plan.destination.name, "my-desk-os-trixie-arm64.iso")
        self.assertEqual(plan.output, Path(tmp).resolve())
        self.assertEqual(plan.mode, "docker")
        self.assertIsNone(plan.daemon)
        self.assertIn("linux-image-arm64", plan.packages)
        self.assertTrue(plan.log)
        self.assertIsNone(plan.workspace)
        self.assertEqual(plan.project, Path("<temporary workspace>/live-os"))
        self.assertEqual(plan.config_command()[0], "lb")

    def test_an_unknown_suite_is_a_warning_not_a_stop(self) -> None:
        with captured() as out:
            plan = self.plan(["--name", "Sid", "--release", "sarge", "--dry-run"])
        self.assertEqual(plan.release, "sarge")
        self.assertIn("not a known Debian suite", out.getvalue())

    def test_a_native_plan_prints_the_commands_it_would_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(["--name", "Nat", "--dry-run", "--output", tmp, "--container", "always"])
            with captured() as out:
                plan.describe()
        text = out.getvalue()
        self.assertIn("mode:       docker", text)
        self.assertIn("apply-staging.sh", text)
        self.assertIn("--iso-application Nat", text.replace("\n", ""))

    def test_free_space_is_only_a_warning(self) -> None:
        plan = self.plan(["--name", "Space", "--dry-run"])
        with unittest.mock.patch.object(build_os, "free_gib", return_value=3.0), captured() as out:
            build_os.check_free_space(plan)
        self.assertIn("only 3.0 GiB free", out.getvalue())
        with unittest.mock.patch.object(build_os, "free_gib", return_value=900.0), captured() as out:
            build_os.check_free_space(plan)
        self.assertEqual(out.getvalue(), "")
        with (
            unittest.mock.patch.object(build_os, "free_gib", return_value=3.0),
            unittest.mock.patch.object(build_os, "RECOMMENDED_FREE_GIB", 0),
            captured() as out,
        ):
            build_os.check_free_space(plan)
        self.assertEqual(out.getvalue(), "")

    def test_free_space_checks_the_workspace_that_will_be_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(
                ["--name", "Space", "--dry-run", "--work-dir", str(Path(tmp) / "deep" / "nested")]
            )
            seen: list[Path] = []

            def record(where: Path) -> float:
                seen.append(where)
                return 1.0

            with unittest.mock.patch.object(build_os, "free_gib", side_effect=record), captured():
                build_os.check_free_space(plan)
        # the plan resolves the work dir, and free space is measured on the first
        # existing ancestor -- /private/var and the long user name on macOS/Windows
        self.assertEqual(seen, [Path(tmp).resolve()])

    def test_skip_checks_silences_the_disk_warning(self) -> None:
        plan = self.plan(["--name", "Skip", "--dry-run", "--skip-checks"])
        with unittest.mock.patch.object(build_os, "free_gib", return_value=0.1), captured() as out:
            build_os.check_free_space(plan)
        self.assertEqual(out.getvalue(), "")

    def test_a_workspace_clears_stale_images_but_never_the_users_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "live-os").mkdir()
            (root / "live-os" / "old.iso").write_bytes(b"old")
            plan = self.plan(["--name", "Ws", "--dry-run", "--work-dir", str(root)])
            workspace = build_os.Workspace.create(plan)
            self.assertFalse((root / "live-os" / "old.iso").exists())
            self.assertEqual(workspace.project, root.resolve() / "live-os")
            self.assertTrue(workspace.log_path is not None)
            with captured() as out:
                workspace.close()  # a user-provided directory is simply left alone
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(root.exists())
            kept_elsewhere = build_os.Workspace.create(
                self.plan(["--name", "Ws", "--dry-run", "--work-dir", str(root), "--keep-work"])
            )
            with captured() as out:
                kept_elsewhere.close()
            self.assertIn("Build workspace kept at", out.getvalue())

    def test_a_temporary_workspace_disappears_unless_kept(self) -> None:
        plan = self.plan(["--name", "Tmp", "--dry-run"])
        workspace = build_os.Workspace.create(plan)
        self.assertTrue(workspace.project.is_dir())
        self.assertFalse(workspace.log_path.exists())
        created = workspace.root
        workspace.close()
        self.assertFalse(created.exists(), "the scratch directory should have been removed")

        kept = build_os.Workspace.create(self.plan(["--name", "Tmp", "--dry-run", "--keep-work"]))
        with captured() as out:
            kept.close()
        self.assertIn("kept at", out.getvalue())
        self.assertTrue(kept.root.exists())
        shutil.rmtree(kept.root, ignore_errors=True)

    def test_report_and_sidecars_describe_the_finished_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.plan(["--name", "Done", "--dry-run", "--output", str(root / "dist")])
            plan.output.mkdir(parents=True)
            iso = root / "live.iso"
            iso.write_bytes(b"x" * 2048)
            workspace = build_os.Workspace(root=root, temporary=False, keep=False, log=True)
            workspace.log_path = root / "build.log"
            workspace.log_path.write_text("building...\n", encoding="utf-8")
            build_os.collect(workspace, plan, iso, "abc123")
            checksum = plan.destination.with_suffix(".iso.sha256")
            self.assertEqual(checksum.read_text(encoding="utf-8"), f"abc123  {plan.destination.name}\n")
            self.assertEqual(
                (root / "dist" / (plan.destination.name + ".log")).read_text(encoding="utf-8"),
                "building...\n",
            )
            with captured() as out:
                build_os.report(plan, "abc123")
        text = out.getvalue()
        for fragment in ("Bootable ISO created", "abc123", "qemu-system-x86_64", "balenaEtcher"):
            self.assertIn(fragment, text)

    def test_main_translates_failures_into_exit_codes(self) -> None:
        """Usage errors, build failures, Ctrl-C and I/O trouble each get their own code."""
        argv = ["--name", "Fail", "--container", "always"]

        def run_main(**patches: object) -> tuple[int, str]:
            with (
                unittest.mock.patch.object(build_os, "select_engine", return_value=("docker", None)),
                unittest.mock.patch.object(build_os, "run_build", **patches),
                captured() as out,
            ):
                return build_os.main(list(argv)), out.getvalue()

        code, text = run_main(side_effect=build_os.BuildError("boom"))
        self.assertEqual(code, 1)
        self.assertIn("os-maker: boom", text)
        self.assertIn("workspace was deleted", text)  # the scratch dir is gone, so say so

        code, text = run_main(side_effect=KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertIn("interrupted", text)

        code, text = run_main(side_effect=OSError("no space left on device"))
        self.assertEqual(code, 1)
        self.assertIn("no space left on device", text)

        code, text = run_main(return_value=0)
        self.assertEqual(code, 0)
        self.assertNotIn("workspace was deleted", text)


class DryRunAndFinishTests(unittest.TestCase):
    """The two ends of a build: the printed plan for a native run, and the boot test."""

    @staticmethod
    def plan(argv: list[str], mode: str = "docker") -> build_os.Plan:
        with unittest.mock.patch.object(build_os, "select_engine", return_value=(mode, None)):
            return build_os.decide(build_os.build_parser().parse_args(argv))

    def test_a_native_plan_lists_the_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(["--name", "Natty", "--output", tmp, "--dry-run"], mode="native")
            with captured() as out:
                plan.describe()
        text = out.getvalue()
        self.assertIn("mode:       native", text)
        self.assertIn("lb config --distribution", text)
        self.assertIn("&& lb build", text)
        with captured() as announced:
            plan.announce()
        self.assertIn("Building natively", announced.getvalue())

    def test_a_plan_announces_the_daemon_it_found(self) -> None:
        with (
            unittest.mock.patch.object(build_os, "select_engine", return_value=("docker", "27.1.1")),
            captured() as out,
        ):
            plan = build_os.decide(build_os.build_parser().parse_args(["--name", "Dock", "--dry-run"]))
            plan.announce()
        self.assertIn("daemon 27.1.1", out.getvalue())
        with (
            unittest.mock.patch.object(build_os, "select_engine", return_value=("docker", None)),
            captured() as out,
        ):
            build_os.decide(build_os.build_parser().parse_args(["--name", "Dock", "--dry-run"])).announce()
        self.assertIn("no daemon here yet", out.getvalue())

    def test_run_build_boots_the_finished_image_when_asked(self) -> None:
        def fake_execute(workspace, _plan):  # stands in for `lb build` / the container
            (workspace.project / "built.iso").write_bytes(b"ISO9660" * 200)
            return build_os.CommandResult(0, ["live-build finished"])

        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(
                ["--name", "Booted", "--output", str(Path(tmp) / "dist"), "--boot-test", "--dry-run"]
            )
            with (
                unittest.mock.patch.object(build_os, "execute", side_effect=fake_execute),
                unittest.mock.patch.object(build_os, "qemu_boot_test", return_value=0) as boot,
                captured() as out,
            ):
                self.assertEqual(build_os.run_build(plan), 0)
            self.assertEqual(boot.call_args.args[1:], ("amd64", plan.boot_timeout))
            self.assertEqual(boot.call_args.kwargs["log_path"].name, "build.log")
            self.assertTrue(plan.destination.is_file(), "the ISO was collected into --output")
            self.assertTrue(plan.destination.with_suffix(".iso.sha256").is_file())
            self.assertIn("Bootable ISO created", out.getvalue())

    def test_a_build_without_logging_writes_no_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(["--name", "Silent", "--output", tmp, "--no-log", "--dry-run"])
            workspace = build_os.Workspace.create(plan)
            try:
                self.assertIsNone(workspace.log_path)
            finally:
                workspace.close()
            self.assertFalse((workspace.root / "build.log").exists())


def subprocess_run(command: list[str], cwd: Path | None = None) -> int:
    import subprocess

    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if completed.returncode != 0:  # pragma: no cover - aids debugging
        print(json.dumps({"cmd": command, "stdout": completed.stdout, "stderr": completed.stderr}))
    return completed.returncode


if __name__ == "__main__":
    unittest.main()
