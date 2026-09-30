# OS Maker — build your own bootable Linux, and play with a mini OS

This repository holds two independent Python programs (no third-party dependencies, Python 3.10+):

| File | What it does |
| --- | --- |
| [`build_os.py`](build_os.py) | Builds a **real bootable Debian-based live ISO** on Linux, macOS and Windows. |
| [`os_maker.py`](os_maker.py) | A **mini operating-system simulator**: a shell and a persistent virtual filesystem that runs anywhere Python does. |

```bash
python3 build_os.py --name MyOS                 # -> dist/myos-bookworm-amd64.iso (+ .sha256 and build log)
python3 os_maker.py                             # -> interactive mini shell, saved to os_maker_data.json
```

---

## 1. `build_os.py` — create a bootable OS image

It is a front-end for Debian's own [`live-build`](https://live-team.pages.debian.net/live-build/) tooling, so the
result is a genuine ISO 9660 hybrid image (BIOS **and** UEFI bootable) — not a simulation, and not a disk image of
your current system.

### How it builds

* **Docker (default, all platforms)** — a Debian container runs `apt-get install live-build`, `lb config`, the
  OS Maker staging step and `lb build`. Your host OS is never modified. Requires Docker Desktop (macOS/Windows,
  with WSL 2) or Docker Engine (Linux); privileged containers must be allowed.
* **Native (Linux only)** — if `lb`, `debootstrap`, `xorriso` and `mksquashfs` are installed and you are root, the
  build runs directly on the host. Choose explicitly with `--container never`; `--container auto` (the default)
  prefers native when it is usable and falls back to Docker.

Everything happens in a scratch workspace (`--work-dir` to pick one, `--keep-work` to keep it). Nothing is written to
USB and no host packages are touched. Expect several GB of free disk space and a working internet connection; a first
build takes roughly 5–20 minutes depending on the package list and the CPU.

### Customising the OS

```bash
python3 build_os.py \
  --name StudyOS \
  --release trixie \
  --architecture amd64 \
  --packages sudo,neofetch,tmux,firefox-esr,xfce4 \
  --copy ./wallpaper.png:/usr/share/backgrounds/wallpaper.png \
  --copy ./config/motd:/etc/motd \
  --hook ./scripts/first-setup.sh \
  --with-firmware \
  --compression zstd \
  --boot-test \
  --output dist
```

| Flag | Meaning |
| --- | --- |
| `--name` | Display name: becomes the ISO label and the hostname of the live system. |
| `--release` | Debian suite (`bookworm`, `trixie`, …). |
| `--architecture` | `amd64` for normal PCs, `arm64` for compatible ARM hardware. |
| `--packages` | Comma-separated list. Omit it to get the arch's kernel plus `live-boot, systemd-sysv, sudo, vim-tiny, less, network-manager`. |
| `--copy SRC:DEST` | Copy a file or directory from your machine into the image at absolute path `DEST` (repeatable). |
| `--hook FILE` | A `live-build` chroot hook, executed as root inside the half-built system (repeatable). |
| `--with-firmware` | Add the `non-free-firmware` archive area (Wi-Fi/GPU firmware). |
| `--compression` | `zstd`/`lz4` build much faster; `xz` (live-build's default) gives smaller ISOs. |
| `--boot-test` | After building, boot the ISO headless in QEMU and fail unless the live system reaches its login prompt. |
| `--cache-dir DIR` | Reuse an apt cache between container builds — big speed-up on rebuilds. |
| `--dry-run` | Print the plan and the exact commands, run nothing. |
| `--container`, `--build-image`, `--work-dir`, `--keep-work`, `--output`, `--no-log`, `--skip-checks`, `--boot-timeout`, `-q`, `--version` | Plumbing; see `--help`. |

The kernel is chosen from the architecture, so an `arm64` image gets `linux-image-arm64`; a `--packages` list that
would produce an unbootable image (wrong or missing kernel) is rejected or fixed with a warning.

Each successful run leaves three files in `--output`: `NAME-RELEASE-ARCH.iso`, its `.sha256` checksum
(verify with `sha256sum -c`), and the full `.log` of the build.

### Trying the image

```bash
qemu-system-x86_64 -cdrom dist/myos-bookworm-amd64.iso -m 2048 -boot d   # or: --boot-test
```

Then test on hardware you can afford to reinstall: write the ISO with `dd`, balenaEtcher or Rufus. `--boot-test`
covers `amd64` only — an `arm64` image needs UEFI firmware in QEMU (`-bios /usr/share/OVMF/...`), so the smoke test
is skipped for it.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `neither native live-build nor a usable Docker daemon was found` | Start Docker Desktop (or `sudo systemctl start docker`, and add your user to the `docker` group). Use `--container never` only on Debian/Ubuntu with the tools installed and root. |
| `the Docker daemon is not reachable` | Docker is installed but not running, or the current user may not talk to it. |
| Build fails inside the container | Re-run with `--keep-work --work-dir ./build-work`, then read `build-work/live-os` and the printed log tail. |
| Workspace files owned by root | A container build writes as root on Linux: `sudo rm -rf <work-dir>/live-os`. |
| `only X GiB free where the workspace lives` | Use `--work-dir` on a bigger volume, or `--skip-checks` to ignore the warning. |
| Nothing printed for a long time | `apt-get`/`debootstrap` are chatty only occasionally; the build log in the workspace still grows. |
| Windows: bind mount refused | Make sure the drive holding `--work-dir`/`--output` is shared with Docker Desktop (Settings → Resources → File sharing). |

---

## 2. `os_maker.py` — a mini OS you can break safely

A virtual filesystem, a shell with quoting, variables and redirection, and a JSON "disk" that is saved atomically
between commands. It cannot touch your real files, which makes it a nice toy for teaching what a shell is.

```console
$ python3 os_maker.py
 ██████╗ ███████╗   M A K E R 2.0.0
██╔═══██╗██╔════╝   a mini operating system that runs in your terminal
██║   ██║███████╗   guest@osmaker, cwd ~
...
guest@osmaker:~$ mkdir -p projects/ideas
guest@osmaker:~$ write projects/ideas/todo.txt ship the iso
guest@osmaker:~$ echo "built on $HOSTNAME" >> projects/ideas/todo.txt
guest@osmaker:~$ cat projects/ideas/todo.txt
guest@osmaker:~$ ls -l projects/ideas
guest@osmaker:~$ tree ~
guest@osmaker:~$ exit
```

29 built-ins: `append cat cd clear cp date df du echo edit env exit help history hostname ls man mkdir mv pwd
rm rmdir stat touch tree uname uptime whoami write`, plus the aliases `ll dir cls logout`.

Supported shell behaviour: single/double quotes and backslash escapes, `~`, `$VAR`, `${VAR}`, `$?`,
`>` and `>>` redirection, `-` for `cd`, flags such as `rm -rf` and `cp -r`, and realistic error messages
(`ls: /nope: No such file or directory`). Not supported: pipes, `<`, `&`, command lists (`;`, `&&`) and globs —
each line is exactly one command, and `*` expansion would need a filesystem to expand against.

Run modes:

```bash
python3 os_maker.py                        # interactive (readline history + tab completion when available)
python3 os_maker.py -e "ls /" -e "df"      # run commands and exit (scriptable, no TTY needed)
python3 os_maker.py --reset                # new filesystem
python3 os_maker.py --no-save              # throwaway session
python3 os_maker.py --save-file /tmp/disk.json --ascii
```

The disk lives in `os_maker_data.json` (git-ignored). A corrupt or hand-mangled file is detected on load, reported,
and replaced by a fresh filesystem instead of crashing. `edit <file>` reads lines until a lone `.`, and works in a
pipe too.

---

## Development

```bash
python3 -m unittest discover -s tests -t .      # 206 tests, stdlib only, no Docker/network needed
python3 -m compileall -q build_os.py os_maker.py

pip install ruff mypy "coverage[toml]"           # the three gates CI also runs
ruff check . && ruff format --check .
python3 -m mypy                                  # --strict, configured in pyproject.toml
python3 -m coverage run -m unittest discover -s tests -t . && python3 -m coverage report
```

`os_maker.py` is layered on purpose: `lex`/`parse` for the command line, `FileSystem` for the node tree and the
save file, `TinyOS` for the shell (expansion, dispatch, history), then readline and the CLI. `build_os.py` decides
everything into a `Plan` first and only then runs `Workspace` → `stage` → `execute` → `collect` → `report`, so a bad
flag never survives to minute twenty of a build.

The suite covers the shell's lexer, path handling and every built-in, plus the builder's validation, command
assembly, staging tree and — through a stub `lb` — the complete build → ISO → checksum → log pipeline and the QEMU
boot test (93% branch coverage; CI fails below 85%). CI (`.github/workflows/ci.yml`) lints, type-checks, runs the
tests on Linux/macOS/Windows for Python 3.10 and 3.12, and on pushes to `main` also builds a minimal ISO in Docker
and boots it in QEMU.

Both scripts are single files with `--help` for every flag; nothing else needs to be installed.
