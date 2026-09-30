#!/usr/bin/env python3
"""Cross-platform Python front-end for building a bootable Debian Live ISO."""
from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_PACKAGES = ["linux-image-amd64", "live-boot", "systemd-sysv", "sudo", "vim-tiny", "less", "network-manager"]


def run(command: list[str], cwd: Path) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a bootable Debian-based OS ISO")
    parser.add_argument("--name", default="MyOS", help="OS display name")
    parser.add_argument("--release", default="bookworm", help="Debian release (bookworm or trixie)")
    parser.add_argument("--architecture", default="amd64", choices=["amd64", "arm64"], help="Target architecture")
    parser.add_argument("--packages", default=",".join(DEFAULT_PACKAGES), help="Comma-separated Debian packages")
    parser.add_argument("--output", type=Path, default=Path("dist"), help="Directory for the resulting ISO")
    parser.add_argument("--work-dir", type=Path, help="Keep build workspace here")
    parser.add_argument("--keep-work", action="store_true", help="Keep temporary build workspace")
    parser.add_argument("--container", choices=["auto", "always", "never"], default="auto",
                        help="Build in Docker (default: auto, uses Docker when native prerequisites are unavailable)")
    args = parser.parse_args()

    if not args.name.strip() or any(c in args.name for c in "\n\r\0"):
        parser.error("--name must be a non-empty, single-line name")
    packages = list(dict.fromkeys(p.strip() for p in args.packages.split(",") if p.strip()))
    if not packages:
        parser.error("provide at least one package")

    native = platform.system() == "Linux" and shutil.which("lb") is not None and os.geteuid() == 0
    docker_available = shutil.which("docker") is not None
    if args.container == "always":
        use_docker = True
    elif args.container == "never":
        use_docker = False
        if not native:
            print("Native builds require Linux, live-build, and root privileges. Use Docker or --container auto.", file=sys.stderr)
            return 2
    else:
        use_docker = not native

    if use_docker and not docker_available:
        print("Docker is required for this platform/build mode. Install Docker Desktop or Docker Engine, then retry.", file=sys.stderr)
        return 2
    if not use_docker and not native:
        print("Install live-build and run as root, or use --container always.", file=sys.stderr)
        return 2

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    temp_context = None
    if args.work_dir:
        workspace = args.work_dir.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="os-maker-")
        workspace = Path(temp_context.name).resolve()
    project = workspace / "live-os"
    project.mkdir(parents=True, exist_ok=True)
    safe_hostname = "".join(c.lower() if c.isalnum() or c == "-" else "-" for c in args.name).strip("-")[:63] or "myos"

    config = ["lb", "config", "--distribution", args.release, "--architectures", args.architecture,
              "--binary-images", "iso-hybrid", "--debian-installer", "false",
              "--iso-application", args.name, "--iso-volume", args.name[:32]]
    try:
        if not use_docker:
            run(config, project)

        package_file = project / "config" / "package-lists" / "os-maker.list.chroot"
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_text("# Packages installed in the live OS\n" + "\n".join(packages) + "\n", encoding="utf-8")
        hostname_file = project / "config" / "includes.chroot" / "etc" / "hostname"
        hostname_file.parent.mkdir(parents=True, exist_ok=True)
        hostname_file.write_text(safe_hostname + "\n", encoding="utf-8")
        if use_docker:
            # Docker Desktop supports this bind mount on Windows and macOS as well as Linux.
            print("Building inside Debian via Docker; the host OS is not modified.")
            setup = (
                "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                "live-build debootstrap xorriso ca-certificates && "
                + shlex.join(config) + " && lb build"
            )
            run(["docker", "run", "--rm", "--privileged", "-v", f"{workspace}:/work", "-w", "/work/live-os",
                 "debian:bookworm-slim", "bash", "-lc", setup], workspace)
        else:
            run(["lb", "build"], project)

        images = sorted(project.glob("*.iso"))
        if not images:
            raise RuntimeError("live-build completed but produced no ISO")
        destination = output / f"{safe_hostname}-{args.release}-{args.architecture}.iso"
        shutil.copy2(images[0], destination)
        print(f"\nBootable ISO created: {destination} ({destination.stat().st_size / (1024**3):.2f} GiB)")
        print("Test it in a virtual machine before writing it to USB.")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Build failed (exit code {exc.returncode}). Workspace: {project}", file=sys.stderr)
        return exc.returncode or 1
    except (OSError, RuntimeError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_context:
            if args.keep_work:
                temp_context._finalizer.detach()
                print(f"Build workspace kept at: {project}")
            else:
                temp_context.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
