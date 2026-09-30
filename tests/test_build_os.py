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
            self.assertTrue(os.access(hook_copy, os.X_OK))

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
            self.assertTrue(
                (project / "config" / "hooks" / "normal" / "0001-os-maker.hook.chroot").stat().st_mode & 0o111
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


def subprocess_run(command: list[str], cwd: Path | None = None) -> int:
    import subprocess

    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if completed.returncode != 0:  # pragma: no cover - aids debugging
        print(json.dumps({"cmd": command, "stdout": completed.stdout, "stderr": completed.stderr}))
    return completed.returncode


if __name__ == "__main__":
    unittest.main()
