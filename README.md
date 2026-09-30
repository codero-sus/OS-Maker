# OS Maker — create a bootable Linux OS

OS Maker is a Python front-end for Debian Live Build. It creates a real bootable Debian-based Linux ISO; it is not a simulation. It supports **Linux, macOS, and Windows** by building in a Debian Docker container (Docker Desktop on macOS/Windows). On Linux, it can instead use locally installed Debian build tools.

## Cross-platform setup

1. Install Python 3.10+ and Docker Desktop / Docker Engine. Start Docker and ensure the Docker CLI works (`docker version`). On Windows, use WSL 2-backed Docker Desktop.
2. From this repository, run:

```bash
python build_os.py --name MyOS
```

The default `--container auto` uses a native build only on Linux when running as root with `live-build` installed; otherwise it uses Docker. The resulting ISO is copied to `dist/`. The first build needs internet access and several gigabytes of free disk space. Docker must be permitted to run privileged containers for the Debian image build.

Customize the OS:

```bash
python build_os.py \
  --name StudyOS \
  --release bookworm \
  --architecture amd64 \
  --packages linux-image-amd64,live-boot,sudo,firefox-esr,xfce4 \
  --output dist
```

Use `--container always` to force Docker, or `--container never` to require native Linux tools and root. Set `--work-dir ./build-work --keep-work` to retain the build workspace. `amd64` is for typical Intel/AMD PCs; `arm64` is for compatible ARM hardware. Test the ISO in QEMU or VirtualBox before using it on real hardware. Building does not write to USB or alter your installed OS.

## Python shell demo

`os_maker.py` is an initial virtual shell/filesystem prototype. It is separate from the actual ISO builder (`build_os.py`).
