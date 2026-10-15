# OS Maker — build your own bootable Linux, and play with a mini OS

This repository holds three independent Python programs (no third-party dependencies, Python 3.10+):

| File | What it does |
| --- | --- |
| [`os_builder.py`](os_builder.py) + [`os_builder_gui.py`](os_builder_gui.py) | An **OS builder**: pick a name, a look, packages and files, press a button, get a bootable ISO — or run the same recipe headless from a terminal. |
| [`build_os.py`](build_os.py) | Builds a **real bootable Debian-based live ISO** on Linux, macOS and Windows. |
| [`os_maker.py`](os_maker.py) | A **mini operating-system simulator**: a shell and a persistent virtual filesystem that runs anywhere Python does. |

```bash
python3 os_builder.py                             # -> the GUI: design an OS, build it, watch the log
python3 build_os.py --name MyOS                   # -> dist/myos-bookworm-amd64.iso (+ .sha256 and build log)
python3 os_maker.py                               # -> interactive mini shell, saved to os_maker_data.json
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

## 3. `os_builder.py` — an OS builder with a GUI and a headless CLI

A window, a recipe, and a real ISO. `os_builder.py` is the design studio on top of
`build_os.py`: you choose what the machine is called, what it looks like, what is in it and
how it behaves when it boots; it generates the artwork and runs the actual build. Every
button has an equivalent flag, so the same recipe works over SSH, in CI, or in a container
with no display.

```bash
python3 os_builder.py                       # the GUI (needs Tk, see below)
python3 os_builder.py --preset gaming       # start the GUI from a preset
python3 os_builder.py -r examples/aurora-desktop.json
python3 os_builder.py --gui-check           # can this machine show the GUI, and if not, why
```

![the eleven wallpaper styles the builder can generate](docs/wallpaper-styles.png)

Those eleven wallpapers — `gradient`, `sunset`, `rays`, `grid`, `waves`, `aurora`, `mesh`,
`dots`, `stripes`, `spiral` and `nebula` — are rendered by `os_builder.py` itself with `zlib`
and `struct`: no Pillow, no templates, no image files to edit. `dots`, `mesh`, `nebula`,
`aurora`, `spiral` and `waves` also take a `wallpaper_seed`, so the same style rolls a
different picture; the same seed always draws the same one. The sheet above is generated,
not pasted in, which is the only way it cannot lie about the output:

```bash
python3 tools/style_sheet.py                # redraws docs/wallpaper-styles.png
```

### What you can set

| Tab | Controls |
| --- | --- |
| identity | product name, tagline, hostname, `/etc/motd`, notes, Debian release / architecture / compression / container mode |
| software | preset, the effective package list, ~25 tickable suggestions, your own additions, firmware switch |
| system | locale, console keyboard, timezone, kernel flavour (`auto`, `generic`, `cloud`, `realtime`, `none`), background fit, GRUB menu timeout, OS version / author / home URL, extra apt sources, packages to purge, the `/etc/os-maker.json` build record |
| appearance | accent colour (7 swatches or any hex), eleven wallpaper styles, resolution, a seed, whether the name is stamped on the image, whether `/etc/motd` gets the banner, and a **live preview** that redraws as you type |
| files & hooks | files copied to a path inside the image, shell hooks live-build runs while building |
| build | output/work/cache folders, QEMU boot test and how long to wait, keep-workspace, skip-checks, and a table of what the build produced with SHA-256 verification |

The window adopts the accent colour of the OS you are building, so a "Neon Arcade" recipe
really does look like one. A live log pane shows the builder's output (colour-coded,
auto-scrolling, `Stop` available), and a dry run prints the exact `build_os.py` command line
before committing to a build. `F1` lists the shortcuts; `Alt-1`…`Alt-6` jump between tabs,
`Ctrl-E` shows the build command, `Escape` stops a build.

### The same thing from a terminal

```bash
python3 os_builder.py --new --out my-os.json --preset server   # write a starter recipe
python3 os_builder.py --check my-os.json                        # validate it (exit 0 = buildable)
python3 os_builder.py --preview my-os.json                      # argv + the builder's dry-run plan
python3 os_builder.py --build my-os.json                        # build it with no GUI at all
python3 os_builder.py --wallpaper my-os.json --out wp.png       # just the generated artwork
python3 os_builder.py --list-presets                            # eleven starting points
python3 os_builder.py --preset-info rescue                      # one preset in full
python3 os_builder.py --explain                                 # what every recipe field turns into
```

For scripts and CI:

```bash
python3 os_builder.py --check my-os.json --json       # one JSON object: digest, problems, argv
python3 os_builder.py --build my-os.json --json --quiet --tail 40
python3 os_builder.py --build my-os.json --stall-notice 300
python3 os_builder.py --fix my-os.json --out tidy.json
python3 os_builder.py --diff release-candidate.json nightly.json
python3 os_builder.py --batch examples/
```

`--fix` tidies what a hand edit always drifts into — stray spaces, `#4F9CF9` in upper case,
`1280X720`, duplicated packages, a `hostname` that repeats what the name already implies —
prints every change, and only writes a file if you pass `--out`. `--diff` exits 1 when two
recipes differ, which makes "did this commit change the image?" a one-line CI check.
`--batch` checks a whole folder and returns the worst exit code. `--tail N` is the
compromise for a quiet build: the log stays hidden, but the last N lines are repeated when it
fails, next to a hint if the failure is one the builder recognises.

A build server can set the plumbing once instead of editing every recipe:
`OS_BUILDER_OUTPUT`, `OS_BUILDER_WORK`, `OS_BUILDER_CACHE`, `OS_BUILDER_CONTAINER` and
`OS_BUILDER_TIMEOUT` are applied to any field the recipe leaves blank, in the GUI and in the
CLI alike.

### A recipe is JSON

A recipe is a plain JSON file, so it can be reviewed, versioned and reproduced without ever
opening the GUI:

```json
{
  "name": "Neon Arcade",
  "tagline": "thirty years of games, one reboot",
  "preset": "gaming",
  "packages": ["fceux", "mame-tools"],
  "remove_packages": ["bluetooth"],
  "accent": "#bc8cff",
  "wallpaper_style": "rays",
  "wallpaper_seed": 1985,
  "locale": "en_GB.UTF-8",
  "keyboard": "gb",
  "timezone": "Europe/London",
  "kernel": "generic",
  "version": "1.0",
  "author": "Ada",
  "home_url": "https://example.org/neon-arcade",
  "grub_timeout": 0,
  "motd_art": true,
  "with_branding_record": true,
  "boot_test": true,
  "boot_timeout": 420,
  "files": ["banner.txt:/etc/issue.net"]
}
```

Three ready-made recipes live in [`examples/`](examples) and CI checks all of them
(`--batch examples/`). Relative paths in `files` resolve against the recipe's own folder, so
an example stays portable. Every field is optional: the loader rejects a *type* it cannot use
and names the field, and `--explain` prints what each one turns into.

### How it is put together

The GUI never reimplements the build. `RecipeModel` holds the state, `make_branding()` generates the
wallpaper plus `/etc/motd`, `/etc/issue`, `/etc/issue.net`, `/etc/os-release` and `/etc/os-maker.json`
(which travel as ordinary `--copy` entries) and one live-build `chroot` hook for everything a file copy
cannot do — set `/etc/hostname`, generate the locale and console keymap, point `/etc/localtime`, trim
`GRUB_TIMEOUT`, drop extra apt sources, purge the packages you asked to lose, and register the wallpaper
with GNOME's dconf and XFCE's XML profile so the branding is actually *shown* rather than merely present
— and `BuildRun` runs `build_os.py` as a subprocess with the flags the recipe describes, which is exactly
what `--build` does from a terminal. That means:

* the same recipe works headless, in CI and over SSH, where there is no display;
* `build_os.py` keeps a single supported entry point, so nothing can drift between the two;
* the hook is *executed* by the tests, against a fake rootfs and a fake `apt-get`, so its shell syntax
  and its effects are proven rather than eyeballed;
* the GUI is a thin view: 94% of it is covered by tests that run without Tk, using a small fake
  `tkinter`. Progress comes from live-build's own stage markers and apt's percentages, so the bar
  means something rather than spinning.

**Needs Tk 8.6** (for PNG previews). Debian/Ubuntu: `sudo apt install python3-tk`; macOS: `brew install
python-tk`; Windows: reinstall Python with *tcl/tk and IDLE* ticked. Without it the GUI says so and
points at `--preview` / `--build`, which need nothing extra.

---

## Development

```bash
python3 -m unittest discover -s tests -t .      # 463 tests, stdlib only, no Docker/network needed
python3 -m compileall -q build_os.py os_maker.py os_builder.py os_builder_gui.py tools/style_sheet.py

pip install ruff mypy "coverage[toml]"           # the three gates CI also runs
ruff check . && ruff format --check .
python3 -m mypy                                  # --strict, configured in pyproject.toml
python3 -m coverage run -m unittest discover -s tests -t . && python3 -m coverage report
```

The four programs are deliberately layered: `os_builder_gui.py` only draws widgets and
`os_builder.py` holds every decision (recipe, validation, branding, argv, progress, the recipe tools) so the GUI is
testable without a display; `RecipeModel` is the whole application. `tools/style_sheet.py` is the one non-script:
it exists so the README image is generated by the code it documents.

`os_maker.py` is layered on purpose: `lex`/`parse` for the command line, `FileSystem` for the node tree and the
save file, `TinyOS` for the shell (expansion, dispatch, history), then readline and the CLI. `build_os.py` decides
everything into a `Plan` first and only then runs `Workspace` → `stage` → `execute` → `collect` → `report`, so a bad
flag never survives to minute twenty of a build.

The suite covers the shell's lexer, path handling and every built-in (including the error text each command gives
on bad input), plus the builder's validation, engine probing, command assembly, staging tree and — through a stub
`lb` — the complete build → ISO → checksum → log pipeline and the QEMU boot test. Branch coverage is 95%
(`build_os.py` 99%, `os_builder.py` 95%, `os_builder_gui.py` 94%, `os_maker.py` 95%); CI fails the build below 90%,
so the number is a ratchet rather than a boast.

[`IMPROVEMENTS.md`](IMPROVEMENTS.md) is the ledger of what changed and which test proves each line — one row per
real behaviour, nothing counted twice, and an explicit list of what remains unverified.

CI (`.github/workflows/ci.yml`) runs four gates: ruff lint + format, `mypy --strict`, the tests on
Linux/macOS/Windows for Python 3.10 and 3.12, and a coverage job that fails below 90% so the number can only go
up. On pushes to `main` it also builds a minimal ISO in Docker and boots it in QEMU.

Both scripts are single files with `--help` for every flag; nothing else needs to be installed.
