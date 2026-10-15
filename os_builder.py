#!/usr/bin/env python3
"""OS Builder: a graphical front-end that turns a recipe into a bootable live ISO.

The GUI is a thin, friendly layer over :mod:`build_os` -- it never reimplements the
build.  A *recipe* (name, release, packages, appearance, files) is validated here,
branded artwork is generated here, and the ISO itself is produced by running
``build_os.py`` as a subprocess with the flags the recipe describes.  One file stays
responsible for how an image is built, and everything in here is checkable without a
display:

    python3 os_builder.py                           # the GUI
    python3 os_builder.py --new --recipe-out my.json  # starter recipe
    python3 os_builder.py --check my.json            # validate and exit
    python3 os_builder.py --preview my.json          # argv + the builder's dry-run plan
    python3 os_builder.py --build my.json            # build from a terminal, no GUI
    python3 os_builder.py --wallpaper my.json        # just the generated artwork
    python3 os_builder.py --list-presets             # what the recipes start from

Structure: :class:`Recipe` (data + JSON schema) -> :func:`recipe_to_argv` (the flags)
-> :func:`make_branding` (wallpaper, motd, issue, os-release, hook) ->
:class:`BuildRun` (subprocess, live progress) -> :class:`os_builder_gui.BuilderGUI`
(Tk, imported only when a GUI is actually wanted).

The wallpaper is a hand-written PNG (:mod:`zlib` and :mod:`struct`, no Pillow), and Tk
8.6 can read PNGs natively, so the preview needs nothing installed either.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import queue
import random
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

__version__ = "1.0.0"

RECIPE_VERSION = 1
HERE = Path(__file__).resolve().parent
BUILD_SCRIPT = HERE / "build_os.py"

RELEASES = ("bookworm", "trixie", "forky", "sid")
ARCHITECTURES = ("amd64", "arm64")
COMPRESSIONS = ("zstd", "xz", "gzip", "lz4", "none")
CONTAINERS = ("auto", "always", "never")
ACCENTS = ("#4f9cf9", "#39d353", "#f78166", "#bc8cff", "#f2cc60", "#ff6ac1", "#79c0ff")
# The GUI's tab order, in one place so the view and the tests cannot disagree.
TAB_NAMES = ("identity", "software", "system", "appearance", "files & hooks", "build")

WALLPAPER_STYLES = (
    "gradient",
    "sunset",
    "rays",
    "grid",
    "waves",
    "aurora",
    "mesh",
    "dots",
    "stripes",
    "spiral",
    "nebula",
)
# What a desktop calls "how the picture fills the screen"; the names are GNOME's.
PICTURE_OPTIONS = ("zoomed", "wallpaper", "scaled", "stretched", "centered", "spanned")
# XFCE's numeric equivalent of the same choice, for the XML profile the hook writes.
XFCE_IMAGE_STYLE = {"zoomed": 5, "wallpaper": 0, "scaled": 1, "stretched": 2, "centered": 4, "spanned": 3}
KERNELS = ("auto", "generic", "cloud", "realtime", "none")
KERNEL_PACKAGES = {
    "amd64": {
        "generic": "linux-image-amd64",
        "cloud": "linux-image-cloud-amd64",
        "realtime": "linux-image-rt-amd64",
    },
    "arm64": {
        "generic": "linux-image-arm64",
        "cloud": "linux-image-cloud-arm64",
        "realtime": "linux-image-rt-arm64",
    },
}
DEFAULT_HOME_URL = "https://github.com/codero-sus/OS-Maker"
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
PACKAGE_RE = re.compile(r"[A-Za-z0-9+._~-]+(?::[A-Za-z0-9+._~-]+)?")
LOCALE_RE = re.compile(r"^[a-z]{2}(?:_[A-Z]{2})?(?:\.[A-Za-z0-9-]{1,14})?(?:@[A-Za-z]{1,8})?$")
KEYMAP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]{1,32}){1,2}$")
URL_RE = re.compile("^https?://[^\\s'\"<>`]+$", re.IGNORECASE)
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
SOURCE_LINE_RE = re.compile(r"^(?:deb|deb-src) (?:\[[^\]]*\] )?https?://\S+ \S+(?: \S+)+$")
ONE_LINE_MAX = 80
SIZE_RE = re.compile(r"^(\d{2,5})\s*[xX*]\s*(\d{2,5})$")


class RecipeError(ValueError):
    """A recipe problem the user can act on; the text is shown verbatim."""


# --------------------------------------------------------------------------- #
# presets: known-good Debian selections, editable in the GUI
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    blurb: str
    packages: tuple[str, ...]
    desktop: str | None = None
    firmware: bool = False
    accent: str = ACCENTS[0]
    wallpaper: str = "gradient"


PRESETS: dict[str, Preset] = {
    preset.key: preset
    for preset in (
        Preset(
            "minimal",
            "Minimal live system",
            "Quick to build, boots to a shell. Good for a USB rescue stick or for testing hooks.",
            ("htop", "net-tools", "iputils-ping", "curl"),
            accent="#79c0ff",
            wallpaper="grid",
        ),
        Preset(
            "desktop",
            "Friendly desktop",
            "Debian's own desktop task metapackage: a whole desktop, terminal and file manager.",
            ("task-desktop", "xterm", "fonts-dejavu", "curl", "git"),
            desktop="task-desktop",
            accent="#4f9cf9",
            wallpaper="gradient",
        ),
        Preset(
            "workstation",
            "Developer workstation",
            "GNOME plus compilers, git and the tools you install first on any new machine.",
            ("task-gnome-desktop", "build-essential", "git", "python3-pip", "vim", "tmux", "jq", "htop"),
            desktop="task-gnome-desktop",
            accent="#39d353",
            wallpaper="waves",
        ),
        Preset(
            "server",
            "Admin rescue ISO",
            "SSH plus disks, filesystems and containers: boot a machine you cannot boot and fix it.",
            (
                "task-ssh-server",
                "git",
                "htop",
                "parted",
                "gparted",
                "e2fsprogs",
                "dosfstools",
                "smartmontools",
                "testdisk",
            ),
            accent="#f78166",
            wallpaper="grid",
        ),
        Preset(
            "gaming",
            "Retro and arcade",
            "A slim X session with emulators and the free classics, firmware included.",
            ("task-x11", "mesa-utils", "retroarch", "supertux", "neverputt", "0ad"),
            desktop="task-x11",
            firmware=True,
            accent="#bc8cff",
            wallpaper="rays",
        ),
        Preset(
            "media",
            "Living-room media box",
            "Boots on the television: Kodi, mpv, VLC and the codecs, on a desktop a remote drives.",
            ("task-xfce-desktop", "kodi", "mpv", "vlc", "ffmpeg", "fonts-dejavu"),
            desktop="task-xfce-desktop",
            firmware=True,
            accent="#f78166",
            wallpaper="nebula",
        ),
        Preset(
            "creative",
            "Studio workstation",
            "Images, vector, 3D, audio and video editing, with the fonts and codecs that go with them.",
            (
                "task-gnome-desktop",
                "gimp",
                "inkscape",
                "blender",
                "audacity",
                "kdenlive",
                "fonts-firacode",
            ),
            desktop="task-gnome-desktop",
            accent="#ff6ac1",
            wallpaper="mesh",
        ),
        Preset(
            "office",
            "Everyday office",
            "LibreOffice, a browser, email and an ebook reader: a whole desk in one ISO.",
            ("task-mate-desktop", "libreoffice", "firefox-esr", "thunderbird", "calibre"),
            desktop="task-mate-desktop",
            accent="#f2cc60",
            wallpaper="dots",
        ),
        Preset(
            "network",
            "Network workshop",
            "Packet capture, auditing and wireless tools for a machine you boot to look at a network.",
            ("task-x11", "xterm", "nmap", "tcpdump", "tshark", "whois", "bind9-dnsutils"),
            desktop="task-x11",
            firmware=True,
            accent="#79c0ff",
            wallpaper="grid",
        ),
        Preset(
            "classroom",
            "Classroom lab",
            "A desktop with the learning stack: typing, sky, drawing and an office suite.",
            ("task-gnome-desktop", "libreoffice", "gcompris-qt", "stellarium", "tuxtype", "tuxmath"),
            desktop="task-gnome-desktop",
            accent="#39d353",
            wallpaper="aurora",
        ),
        Preset(
            "kiosk",
            "Single-page kiosk",
            "Boots straight into a fullscreen browser: displays, demos and reception desks.",
            ("task-x11", "xinit", "openbox", "unclutter", "chromium"),
            desktop="task-x11",
            accent="#f2cc60",
            wallpaper="sunset",
        ),
    )
}


# --------------------------------------------------------------------------- #
# the recipe
# --------------------------------------------------------------------------- #
@dataclass
class Recipe:
    """Everything the builder needs to know, and nothing it does not."""

    name: str = "Fantastic OS"
    tagline: str = "Built with OS Maker"
    hostname: str = ""
    motd: str = ""
    release: str = "trixie"
    architecture: str = "amd64"
    preset: str = "desktop"
    packages: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # "SOURCE:DEST-IN-IMAGE"
    hooks: list[str] = field(default_factory=list)
    compression: str = "zstd"
    with_firmware: bool = False
    with_wallpaper: bool = True
    container: str = "auto"
    boot_test: bool = False
    boot_timeout: int = 300  # only used with boot_test; QEMU is slow on shared runners
    keep_work: bool = False
    skip_checks: bool = False
    accent: str = ACCENTS[0]
    wallpaper_style: str = "gradient"
    wallpaper_size: str = "1920x1080"
    output_dir: str = ""
    work_dir: str = ""
    cache_dir: str = ""
    notes: str = ""
    # identity, advertised the way the distribution does it
    version: str = ""  # empty means "1.0", which is what os-release gets
    author: str = ""
    home_url: str = ""
    # how the machine behaves once it is installed or booted
    locale: str = ""
    keyboard: str = ""
    timezone: str = ""
    kernel: str = "auto"
    grub_timeout: int = 5
    # extra software decisions the builder can express but Debian's CLI cannot
    apt_sources: list[str] = field(default_factory=list)
    remove_packages: list[str] = field(default_factory=list)
    # appearance details that reach the desktop, not just the picture
    picture_options: str = "zoomed"
    show_wordmark: bool = True
    motd_art: bool = False
    wallpaper_seed: int = 0
    with_branding_record: bool = True

    def __post_init__(self) -> None:
        # Not a field: relative paths in a recipe resolve against the folder the recipe
        # was read from, which is a property of the file, not of the recipe's contents.
        self.base_dir: Path = Path.cwd()

    # -- reading and writing ------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: object) -> Recipe:
        if not isinstance(data, dict):
            raise RecipeError("a recipe must be a JSON object")
        version = data.get("recipe_version", RECIPE_VERSION)
        if isinstance(version, int) and version > RECIPE_VERSION:
            raise RecipeError(f"recipe version {version} is newer than this builder ({RECIPE_VERSION})")
        fields = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - fields - {"recipe_version"})
        if unknown:
            raise RecipeError(f"unknown recipe key(s): {', '.join(unknown)}")
        recipe = cls(**{key: value for key, value in data.items() if key in fields})
        for name in ("packages", "files", "hooks", "apt_sources", "remove_packages"):
            entries = getattr(recipe, name)
            if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                raise RecipeError(f"'{name}' must be a list of strings")
        for name in (
            "name",
            "tagline",
            "hostname",
            "motd",
            "release",
            "architecture",
            "preset",
            "compression",
            "container",
            "accent",
            "wallpaper_style",
            "wallpaper_size",
            "output_dir",
            "work_dir",
            "cache_dir",
            "notes",
            "version",
            "author",
            "home_url",
            "locale",
            "keyboard",
            "timezone",
            "kernel",
            "picture_options",
        ):
            if not isinstance(getattr(recipe, name), str):
                raise RecipeError(f"'{name}' must be a string")
        for name in (
            "with_firmware",
            "with_wallpaper",
            "boot_test",
            "keep_work",
            "skip_checks",
            "show_wordmark",
            "motd_art",
            "with_branding_record",
        ):
            if not isinstance(getattr(recipe, name), bool):
                raise RecipeError(f"'{name}' must be true or false")
        # bool is an int in Python, so it has to be ruled out first or "boot_timeout":
        # true would load as 1 second and waste someone's afternoon
        for name, shape in (
            ("boot_timeout", "a whole number of seconds"),
            ("grub_timeout", "a whole number of seconds"),
            ("wallpaper_seed", "a whole number"),
        ):
            value = getattr(recipe, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RecipeError(f"'{name}' must be {shape}")
        return recipe

    @classmethod
    def from_json(cls, text: str) -> Recipe:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RecipeError(f"the recipe is not valid JSON: {exc}") from None
        return cls.from_dict(data)

    @classmethod
    def from_file(cls, path: Path) -> Recipe:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RecipeError(f"cannot read {path} ({exc})") from None
        recipe = cls.from_json(text)
        recipe.base_dir = Path(os.path.normpath(str(path.parent.absolute())))
        return recipe

    @classmethod
    def for_preset(cls, key: str) -> Recipe:
        """A recipe filled in from a preset, ready to edit."""
        preset = PRESETS.get(key)
        if preset is None:
            raise RecipeError(f"no preset called {key!r} (try: {', '.join(PRESETS)})")
        return cls(
            name=preset.label,
            tagline=preset.blurb,
            hostname=hostname_for(preset.label),
            preset=preset.key,
            packages=list(preset.packages),
            with_firmware=preset.firmware,
            accent=preset.accent,
            wallpaper_style=preset.wallpaper,
            motd=default_motd(preset.label, preset.blurb),
        )

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = asdict(self)
        data["recipe_version"] = RECIPE_VERSION
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n"

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.to_json(), encoding="utf-8")
        except OSError as exc:
            raise RecipeError(f"cannot write {path} ({exc})") from None

    # -- derived ------------------------------------------------------------ #
    @property
    def resolved_hostname(self) -> str:
        return self.hostname or hostname_for(self.name)

    @property
    def display_motd(self) -> str:
        body = self.motd or default_motd(self.name, self.tagline)
        if not self.motd_art:
            return body
        banner = wordmark_art(self.name.strip(), limit=12)
        return f"{banner}\n{body}" if banner else body

    @property
    def pretty_name(self) -> str:
        """What os-release shows: the name, plus the recipe's version when it has one."""
        name = self.name.strip()
        return f"{name} {self.version}" if self.version.strip() else name

    @property
    def kernel_package(self) -> str:
        """The kernel this recipe asks for, or "" to let Debian's default stand."""
        if self.kernel not in KERNELS or self.kernel in ("auto", "none"):
            return ""
        return KERNEL_PACKAGES.get(self.architecture, {}).get(self.kernel, "")

    def digest(self) -> str:
        """A stable hash of these settings: same recipe, same digest, any machine."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def branding_record(self) -> str:
        """The machine-readable card the image carries, so a guest can say who built it."""
        record = {
            "name": self.name.strip(),
            "version": self.version.strip() or "1.0",
            "tagline": self.tagline.strip(),
            "hostname": self.resolved_hostname,
            "preset": self.preset,
            "release": self.release,
            "architecture": self.architecture,
            "accent": self.accent,
            "wallpaper_style": self.wallpaper_style,
            "wallpaper_size": self.wallpaper_size,
            "locale": self.locale,
            "timezone": self.timezone,
            "recipe_digest": self.digest(),
            "builder": f"os_builder.py {__version__}",
        }
        return json.dumps(record, indent=2, ensure_ascii=False) + "\n"

    @property
    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir).expanduser() if self.output_dir else HERE / "dist"

    @property
    def preset_packages(self) -> tuple[str, ...]:
        preset = PRESETS.get(self.preset)
        return preset.packages if preset else ()

    def merged_packages(self) -> list[str]:
        """The preset's packages, the kernel the recipe asks for and the user's own.

        De-duplicated, order preserved: a list you can read back off the command line.
        """
        wanted = (self.kernel_package, *self.preset_packages, *self.packages)
        seen: dict[str, None] = {}
        for package in wanted:
            name = package.strip()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    def problems(self) -> list[str]:
        """Why this recipe should not be run yet; empty means ready to build."""
        problems: list[str] = []
        name = self.name.strip()
        if not name:
            problems.append("give the OS a name")
        elif len(name) > 60:
            problems.append("the name is longer than 60 characters")
        elif any(char in name for char in "\r\n\0"):
            problems.append("the name has to fit on one line")
        if self.release not in RELEASES:
            problems.append(f"release {self.release!r} is not one of {', '.join(RELEASES)}")
        if self.architecture not in ARCHITECTURES:
            problems.append(f"architecture {self.architecture!r} must be one of {', '.join(ARCHITECTURES)}")
        if self.compression not in COMPRESSIONS:
            problems.append(f"compression {self.compression!r} must be one of {', '.join(COMPRESSIONS)}")
        if self.container not in CONTAINERS:
            problems.append(f"container mode {self.container!r} must be one of {', '.join(CONTAINERS)}")
        if self.preset not in PRESETS:
            problems.append(f"preset {self.preset!r} is unknown (see --list-presets)")
        if self.wallpaper_style not in WALLPAPER_STYLES:
            problems.append(
                f"wallpaper style {self.wallpaper_style!r} must be one of {', '.join(WALLPAPER_STYLES)}"
            )
        if parse_size(self.wallpaper_size) is None:
            # two different mistakes deserve two different sentences: a shape that is
            # not a size at all, and a size the renderer will not draw
            if SIZE_RE.match(self.wallpaper_size.strip()):
                problems.append(f"wallpaper size {self.wallpaper_size!r} must be between 64x64 and 4096x4096")
            else:
                problems.append(f"wallpaper size {self.wallpaper_size!r} must look like 1920x1080")
        if not HEX_COLOR_RE.match(self.accent):
            problems.append(f"accent colour {self.accent!r} must look like #4f9cf9")
        if not isinstance(self.boot_timeout, int) or not 30 <= self.boot_timeout <= 7200:
            problems.append("boot timeout must be a whole number of seconds between 30 and 7200")
        if self.version and not VERSION_RE.match(self.version.strip()):
            problems.append(f"version {self.version!r} must be short and shell-safe, like 2.1 or 1.0-rc1")
        if self.author.strip() and (
            len(self.author.strip()) > ONE_LINE_MAX or any(char in self.author for char in "\r\n\0")
        ):
            problems.append(f"the author line has to fit on one line, {ONE_LINE_MAX} characters or fewer")
        if self.home_url.strip() and not URL_RE.match(self.home_url.strip()):
            problems.append(f"home URL {self.home_url!r} must be a full http(s) URL with no spaces")
        if self.kernel not in KERNELS:
            problems.append(f"kernel {self.kernel!r} must be one of {', '.join(KERNELS)}")
        if self.kernel not in KERNELS or (self.kernel != "auto" and not self.kernel_package):
            problems.append(
                f"no {self.kernel} kernel is published for {self.architecture}; use 'auto' to let Debian choose"
            )
        if self.picture_options not in PICTURE_OPTIONS:
            problems.append(
                f"background fit {self.picture_options!r} must be one of {', '.join(PICTURE_OPTIONS)}"
            )
        if not isinstance(self.grub_timeout, int) or not 0 <= self.grub_timeout <= 120:
            problems.append("boot menu timeout must be a whole number of seconds between 0 and 120")
        if not isinstance(self.wallpaper_seed, int):
            problems.append("the wallpaper seed must be a whole number")
        for field_name, pattern, shape in (
            ("locale", LOCALE_RE, "like en_GB.UTF-8"),
            ("keyboard", KEYMAP_RE, "a console keymap, like gb or de"),
            ("timezone", TIMEZONE_RE, "like Europe/London"),
        ):
            value = getattr(self, field_name).strip()
            if value and not pattern.match(value):
                problems.append(f"{field_name} {value!r} must be {shape}")
        for line in self.apt_sources:
            if not SOURCE_LINE_RE.match(line.strip()):
                problems.append(
                    f"apt source {line.strip()!r} must be one 'deb https://host suite component' line"
                )
        for package in self.remove_packages:
            if not PACKAGE_RE.fullmatch(package.strip() or "\0"):
                problems.append(f"{package!r} is not a valid Debian package name")
        hostname = self.resolved_hostname
        if not HOSTNAME_RE.match(hostname):
            problems.append(f"hostname {hostname!r} must be 1-63 letters, digits and dashes")
        for package in self.merged_packages():
            if not PACKAGE_RE.fullmatch(package):
                problems.append(f"{package!r} is not a valid Debian package name")
        for entry in self.files:
            source, separator, destination = entry.rpartition(":")
            if not separator or not source.strip() or not destination.strip():
                problems.append(f"file {entry!r} must be SOURCE:DEST-IN-IMAGE")
                continue
            if not self.resolve(source.strip()).exists():
                problems.append(f"file source {source.strip()!r} does not exist")
        for entry in self.hooks:
            if not self.resolve(entry).is_file():
                problems.append(f"hook {entry!r} is not a readable file")
        return problems

    def resolve(self, value: str) -> Path:
        """An absolute path, or one relative to the recipe's own folder."""
        return absolutise_path(value, self.base_dir)

    def ready(self) -> bool:
        return not self.problems()


def parse_size(text: str) -> tuple[int, int] | None:
    """``1920x1080`` -> ``(1920, 1080)``, or None when it does not parse."""
    match = SIZE_RE.match(text.strip())
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        return None
    return width, height


def hostname_for(name: str) -> str:
    """Turn "Fantastic OS" into a legal hostname, the way build_os.py does."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-").strip("-") or "osmaker"
    return cleaned[:63].rstrip("-")


def default_motd(name: str, tagline: str) -> str:
    first = f"Welcome to {name.strip() or 'this system'}."
    middle = tagline.strip() or "Built with OS Maker."
    return "\n".join([first, middle, "", "Every package here was chosen deliberately. Enjoy it."]) + "\n"


def parse_package_list(text: str | Sequence[str]) -> list[str]:
    """Accept a comma- or space-separated string (or a list) and return clean names."""
    chunks: list[str] = []
    for piece in [text] if isinstance(text, str) else list(text):
        chunks.extend(re.split(r"[,\s]+", piece))
    seen: dict[str, None] = {}
    for chunk in chunks:
        name = chunk.strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def recipe_to_argv(
    recipe: Recipe,
    *,
    branding: Iterable[tuple[Path, str]] = (),
    hook: Path | None = None,
    dry_run: bool = False,
) -> list[str]:
    """The build_os.py arguments this recipe describes, plus the generated artwork.

    Branding travels as ordinary ``--copy`` and ``--hook`` entries: the builder keeps
    its own CLI as the only way in, so a recipe can always be reproduced from a terminal.
    """
    argv = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--name",
        recipe.name.strip(),
        "--release",
        recipe.release,
        "--architecture",
        recipe.architecture,
        "--compression",
        recipe.compression,
        "--container",
        recipe.container,
        "--output",
        str(recipe.resolved_output_dir),
    ]
    packages = recipe.merged_packages()
    if packages:
        argv += ["--packages", ",".join(packages)]
    for source, destination in branding:
        argv += ["--copy", f"{source.as_posix()}:{destination}"]
    argv += flat_pairs("--copy", [absolutise(entry, recipe.base_dir) for entry in recipe.files])
    # the recipe's own hooks first: the branding hook only finishes the job, so it
    # must never override something the author wrote themselves
    hooks = [str(recipe.resolve(item)) for item in recipe.hooks]
    if hook is not None:
        hooks.append(str(hook))
    argv += flat_pairs("--hook", hooks)
    argv += flag("--with-firmware", recipe.with_firmware)
    if recipe.boot_test:
        argv += ["--boot-test", "--boot-timeout", str(recipe.boot_timeout)]
    argv += flag("--keep-work", recipe.keep_work)
    argv += flag("--skip-checks", recipe.skip_checks)
    argv += pairs("--work-dir", recipe.work_dir)
    argv += pairs("--cache-dir", recipe.cache_dir)
    if dry_run:
        argv.append("--dry-run")
    return argv


def flag(name: str, enabled: bool) -> list[str]:
    return [name] if enabled else []


def pairs(name: str, value: str) -> list[str]:
    return [name, str(Path(value).expanduser())] if value.strip() else []


def flat_pairs(name: str, values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        out += [name, value]
    return out


def absolutise(entry: str, base_dir: Path) -> str:
    """Rewrite the source half of a ``SOURCE:DEST`` spec as an absolute path.

    ``normpath`` rather than :meth:`Path.resolve` on purpose: resolve() follows
    symlinks, which would quietly rewrite ``/var`` to ``/private/var`` on macOS.
    """
    source, separator, destination = entry.rpartition(":")
    if not separator:
        return str(absolutise_path(source, base_dir)) if source else entry
    return f"{absolutise_path(source.strip(), base_dir).as_posix()}:{destination.strip()}"


def absolutise_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(os.path.normpath(str(base_dir / path)))
    return path


def describe_argv(argv: Sequence[str]) -> str:
    return shlex.join(str(part) for part in argv)


def plan_summary(argv: Sequence[str], *, timeout: int = 180) -> tuple[int, str]:
    """Run the builder with these exact arguments; return its exit code and output.

    The caller includes ``--dry-run`` itself, so the command line the GUI shows is the
    command line that was actually executed.
    """
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=str(HERE),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"os-builder: could not ask the builder for a plan: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr).rstrip()


# --------------------------------------------------------------------------- #
# branding: a PNG wallpaper and the text files, generated with the stdlib only
# --------------------------------------------------------------------------- #
FONT: dict[str, tuple[str, ...]] = {
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("###..", "..#..", "..#..", "..#..", "..#..", "..#..", "###.."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#...#", "#..#.", "###..", "#..#.", "#...#", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#.#.#", "#..##", ".####", "....#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", "..#..", "..#..", "..#..", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "#...#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ",": (".....", ".....", ".....", ".....", ".##..", ".##..", "..#.."),
    ":": (".....", ".....", ".##..", ".##..", ".....", ".##..", ".##.."),
    "-": (".....", ".....", ".....", ".###.", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "/": ("....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "...#.", "..#..", ".##..", ".....", ".##.."),
    "'": ("..#..", "..#..", "..#..", ".....", ".....", ".....", "....."),
    "#": (".#.#.", "#####", ".#.#.", ".#.#.", "#####", ".#.#.", "....."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
    "%": ("##..#", "##.#.", "..#..", ".#.##", "#..##", ".....", "....."),
    "*": (".....", "..#..", "#.#.#", ".###.", "#.#.#", "..#..", "....."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "<": ("...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."),
    ">": (".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."),
}

GLYPH_WIDTH, GLYPH_HEIGHT, GLYPH_TRACKING = 5, 7, 1


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """``#abc`` or ``#aabbcc`` -> ``(r, g, b)``."""
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    if len(digits) != 6:
        raise RecipeError(f"{value!r} is not a hex colour")
    try:
        channels = tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        raise RecipeError(f"{value!r} is not a hex colour") from None
    return channels[0], channels[1], channels[2]


def mix(first: tuple[int, int, int], second: tuple[int, int, int], weight: float) -> tuple[int, int, int]:
    """Linear blend: ``weight`` 0 keeps ``first``, 1 gives ``second``."""
    ratio = max(0.0, min(1.0, weight))
    red = round(first[0] + (second[0] - first[0]) * ratio)
    green = round(first[1] + (second[1] - first[1]) * ratio)
    blue = round(first[2] + (second[2] - first[2]) * ratio)
    return red, green, blue


def text_mask(text: str) -> list[str]:
    """Render ASCII into seven rows of '#' and '.' with the built-in 5x7 face."""
    glyphs = [FONT.get(character.upper(), FONT[" "]) for character in text]
    width = len(glyphs) * GLYPH_WIDTH + max(0, len(glyphs) - 1) * GLYPH_TRACKING
    rows = [["."] * width for _ in range(GLYPH_HEIGHT)]
    for index, glyph in enumerate(glyphs):
        left = index * (GLYPH_WIDTH + GLYPH_TRACKING)
        for row, pattern in enumerate(glyph):
            for column, pixel in enumerate(pattern):
                if pixel == "#":
                    rows[row][left + column] = "#"
    return ["".join(row) for row in rows]


def paint_text(
    pixels: list[list[tuple[int, int, int]]],
    text: str,
    *,
    scale: int,
    colour: tuple[int, int, int],
    anchor_y: float,
    scrim: float = 0.0,
) -> None:
    """Draw one centred line of text into a pixel grid, in place.

    ``scrim`` darkens a soft band behind the glyphs first.  Without it a pale style
    (``sunset`` in gold, ``grid`` at its lit horizon) can swallow the product name,
    and a wallpaper nobody can read is a bug, not a taste question.
    """
    if not text.strip() or scale < 1 or not pixels or not pixels[0]:
        return
    mask = text_mask(text)
    block_width, block_height = len(mask[0]) * scale, len(mask) * scale
    left = (len(pixels[0]) - block_width) // 2
    top = int(len(pixels) * anchor_y) - block_height // 2
    if scrim > 0:
        pad = scale * 2
        for row in range(top - pad, top + block_height + pad):
            if not 0 <= row < len(pixels):
                continue
            fade_y = _ramp(row - (top - pad), block_height + 2 * pad)
            target = pixels[row]
            for column in range(left - pad, left + block_width + pad):
                if not 0 <= column < len(target):
                    continue
                weight = scrim * fade_y * _ramp(column - (left - pad), block_width + 2 * pad)
                red, green, blue = target[column]
                target[column] = (
                    clamp8(red * (1.0 - weight)),
                    clamp8(green * (1.0 - weight)),
                    clamp8(blue * (1.0 - weight)),
                )
    for row, line in enumerate(mask):
        for column, cell in enumerate(line):
            if cell != "#":
                continue
            for dy in range(scale):
                y = top + row * scale + dy
                if not 0 <= y < len(pixels):
                    continue
                target = pixels[y]
                for dx in range(scale):
                    x = left + column * scale + dx
                    if 0 <= x < len(target):
                        target[x] = mix(target[x], colour, 0.94)


def _ramp(distance: int, span: int) -> float:
    """0 at the outer edge, 1 by ``span / 6`` pixels in: a soft band, not a box."""
    if distance < 0 or distance > span:
        return 0.0
    edge = max(1.0, span / 6.0)
    return max(0.0, min(1.0, min(distance, span - distance) / edge))


def wordmark_art(text: str, *, limit: int = 12) -> str:
    """The recipe's name as block ASCII, for a message of the day.

    Uses the wallpaper's own 5x7 face, so the login banner and the desktop agree.
    Anything longer than ``limit`` characters is left out rather than wrapped: a
    broken banner across an 80-column terminal is worse than none.
    """
    name = text.strip()
    if not name or len(name) > limit:
        return ""
    mask = text_mask(name)
    if not mask:
        return ""
    rows = ["".join("##" if pixel == "#" else "  " for pixel in line) for line in mask]
    return "\n".join(row.rstrip() for row in rows).rstrip("\n")


def wallpaper_pixels(recipe: Recipe, width: int, height: int) -> list[list[tuple[int, int, int]]]:
    """The generated wallpaper: rows of ``(r, g, b)``.

    Everything is inlined on purpose -- this loop runs once per pixel of a
    1920x1080 image, and the GUI calls it on every change, so a helper call per
    pixel would triple the time for no visual gain.
    """
    width, height = max(2, width), max(2, height)
    red, green, blue = hex_to_rgb(recipe.accent)
    style = recipe.wallpaper_style
    seed = recipe.wallpaper_seed
    if style == "sunset":
        sky = _sunset_rows(width, height, red, green, blue)
    elif style == "rays":
        sky = _rays_rows(width, height, red, green, blue)
    elif style == "grid":
        sky = _grid_rows(width, height, red, green, blue)
    elif style == "waves":
        sky = _waves_rows(width, height, red, green, blue)
    elif style == "aurora":
        sky = _aurora_rows(width, height, red, green, blue, seed)
    elif style == "mesh":
        sky = _mesh_rows(width, height, red, green, blue, seed)
    elif style == "dots":
        sky = _dots_rows(width, height, red, green, blue, seed)
    elif style == "stripes":
        sky = _stripes_rows(width, height, red, green, blue, seed)
    elif style == "spiral":
        sky = _spiral_rows(width, height, red, green, blue, seed)
    elif style == "nebula":
        sky = _nebula_rows(width, height, red, green, blue, seed)
    else:
        sky = _gradient_rows(width, height, red, green, blue)
    if recipe.with_wallpaper and recipe.show_wordmark:
        title = recipe.name.strip()
        if title:
            paint_text(
                sky,
                title,
                scale=fit_scale(title, width, cap=3 * max(1, width // 300)),
                colour=_rgb(mix((252, 252, 255), (red, green, blue), 0.12)),
                anchor_y=0.44,
                scrim=0.42,
            )
        tagline = recipe.tagline.strip()
        if tagline:
            paint_text(
                sky,
                tagline,
                scale=fit_scale(tagline, width, cap=max(1, width // 460)),
                colour=_rgb(mix((206, 212, 222), (red, green, blue), 0.25)),
                anchor_y=0.60,
                scrim=0.34,
            )
    return sky


def _lattice(rows: int, columns: int, seed: int) -> list[list[float]]:
    """A grid of seeded randoms, sampled bilinearly for the organic styles.

    Seeded rather than random, so a recipe renders the same wallpaper on any machine and
    a screenshot in the README stays true.
    """
    rng = random.Random(seed or 7)
    return [[rng.random() for _ in range(columns + 1)] for _ in range(rows + 1)]


def _sample(lattice: list[list[float]], u: float, v: float) -> float:
    """Bilinear lookup at fractional lattice coordinates, clamped at the edges."""
    rows = len(lattice) - 1
    x = min(max(u, 0.0), len(lattice[0]) - 1.001)
    y = min(max(v, 0.0), rows - 1.001)
    x0, y0 = int(x), int(y)
    fx, fy = x - x0, y - y0
    top = lattice[y0][x0] * (1.0 - fx) + lattice[y0][x0 + 1] * fx
    below = lattice[y0 + 1][x0] * (1.0 - fx) + lattice[y0 + 1][x0 + 1] * fx
    return top * (1.0 - fy) + below * fy


def _tinted(light: float, red: int, green: int, blue: int) -> tuple[int, int, int]:
    """Brightness in 0..1, colour out in 0..255, in the accent.

    The floor is a dark blue rather than black: a wallpaper that goes fully black loses
    the wordmark's scrim and looks like a failed render on a laptop at night.
    """
    value = min(1.0, max(0.0, light))
    return (
        clamp8(int(8 + (red - 8) * value)),
        clamp8(int(10 + (green - 10) * value)),
        clamp8(int(18 + (blue - 18) * value)),
    )


def _aurora_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """Curtains of light leaning across a night sky."""
    drift = _lattice(6, 24, seed)
    brightness = _lattice(4, 8, seed + 11)
    rows = []
    for y in range(height):
        v = y / max(1, height - 1)
        lean = _sample(drift, v * 5.0, 0.5) - 0.5
        glow = 0.35 + 0.65 * _sample(brightness, v * 3.0, 0.5)
        line = []
        for x in range(width):
            shifted = x / max(1, width - 1) + lean * (1.0 + v)
            wave = math.sin(shifted * 6.0 + v * 2.0) + 0.5 * math.sin(shifted * 13.0 - v)
            curtain = max(0.0, 1.0 - abs((shifted + wave * 0.08) % 0.34 - 0.17) * 7.0) ** 2
            light = 0.06 + 0.22 * v + curtain * glow * 0.8
            line.append(_tinted(light, red, green, blue))
        rows.append(line)
    return rows


def _mesh_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """Four corner colours, eased into each other: the cheapest thing that looks designed."""
    rng = random.Random(seed or 3)

    def corner(scale: float, lift: int) -> tuple[int, int, int]:
        return (
            clamp8(int(red * scale) + lift + int(10 * rng.random())),
            clamp8(int(green * scale) + lift),
            clamp8(int(blue * scale) + lift + int(6 * rng.random())),
        )

    # deliberately wide: four corners that nearly agree render as flat filler
    corners = (corner(0.14, 26), corner(1.0, 6), corner(0.34, 10), corner(0.82, 0))
    rows = []
    for y in range(height):
        v = y / max(1, height - 1)
        left = _blend(corners[0], corners[2], v)
        right = _blend(corners[1], corners[3], v)
        line = []
        for x in range(width):
            u = x / max(1, width - 1)
            # eased across, so the middle of the blend has no visible seam
            line.append(_blend(left, right, 0.5 - 0.5 * math.cos(math.pi * u)))
        rows.append(line)
    return rows


def _dots_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """A halftone: dots that swell as they run toward the lower right."""
    step = max(6, width // 48)
    jitter = 1.0 + (seed % 7) * 0.11
    rows = []
    for y in range(height):
        v = y / max(1, height - 1)
        line = []
        for x in range(width):
            u = x / max(1, width - 1)
            gx = (x + jitter * step) % step - step / 2.0
            gy = (y + jitter * step) % step - step / 2.0
            radius = step * (0.1 + 0.42 * ((u + v) / 2.0))
            near = max(0.0, 1.0 - math.hypot(gx, gy) / max(1.0, radius))
            line.append(_tinted(0.05 + near * near * 0.72, red, green, blue))
        rows.append(line)
    return rows


def _stripes_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """Diagonal bands: one accent tone, one deep tone, soft edges."""
    band = max(10, width // (6 + seed % 10))
    rows = []
    for y in range(height):
        fade = 0.45 + 0.55 * (1.0 - y / max(1, height - 1))
        line = []
        for x in range(width):
            phase = ((x + y * 2) % (band * 2)) / float(band)
            stripe = 1.0 if phase < 1.0 else max(0.0, 1.0 - (phase - 1.0) * 2.0)
            line.append(_tinted(0.05 + stripe * fade * 0.8, red, green, blue))
        rows.append(line)
    return rows


def _spiral_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """Rays, wound around a point: the same idea as ``rays`` with somewhere to go."""
    arms = 2 + seed % 4
    centre_x, centre_y = width * 0.5, height * 0.58
    rows = []
    for y in range(height):
        dy = (y - centre_y) / max(1.0, height * 0.5)
        line = []
        for x in range(width):
            dx = (x - centre_x) / max(1.0, width * 0.5)
            radius = min(1.6, math.hypot(dx, dy))
            twist = math.atan2(dy, dx) * arms + radius * 5.0
            band = 0.5 + 0.5 * math.cos(twist)
            line.append(_tinted(band**1.6 * (1.0 - 0.5 * radius) + 0.04, red, green, blue))
        rows.append(line)
    return rows


def _nebula_rows(
    width: int, height: int, red: int, green: int, blue: int, seed: int
) -> list[list[tuple[int, int, int]]]:
    """Two octaves of seeded value noise, plus a scatter of stars."""
    fine = _lattice(14, 26, seed)
    coarse = _lattice(5, 9, seed + 3)
    rows = []
    for y in range(height):
        v = y / max(1, height - 1)
        line = []
        for x in range(width):
            u = x / max(1, width - 1)
            cloud = 0.6 * _sample(coarse, u * 5.0, v * 3.0) + 0.4 * _sample(fine, u * 13.0, v * 7.0)
            # pushed through a power curve, or the whole thing reads as milk rather than gas
            cloud = max(0.0, min(1.0, cloud * 1.45 - 0.22)) ** 1.4
            sparkle = 0.4 if ((x * 73856093) ^ (y * 19349663) ^ (seed * 83492791)) % 98765 == 0 else 0.0
            line.append(_tinted(0.03 + cloud * 0.9 + sparkle, red, green, blue))
        rows.append(line)
    return rows


def fit_scale(text: str, width: int, *, cap: int) -> int:
    """The biggest whole-pixel text scale at which ``text`` still fits the canvas."""
    span = max(1, len(text)) * (GLYPH_WIDTH + GLYPH_TRACKING)
    return max(1, min(cap, int(width * 0.86) // span))


def _rgb(rgb: tuple[float, float, float]) -> tuple[int, int, int]:
    red, green, blue = rgb
    return clamp8(red), clamp8(green), clamp8(blue)


def _blend(first: tuple[int, int, int], second: tuple[int, int, int], weight: float) -> tuple[int, int, int]:
    return (
        clamp8(int(first[0] + (second[0] - first[0]) * weight)),
        clamp8(int(first[1] + (second[1] - first[1]) * weight)),
        clamp8(int(first[2] + (second[2] - first[2]) * weight)),
    )


def _gradient_rows(
    width: int, height: int, red: int, green: int, blue: int
) -> list[list[tuple[int, int, int]]]:
    """A diagonal wash with a soft light source in the upper right."""
    top = (clamp8(red * 52 // 100), clamp8(green * 52 // 100), clamp8(blue * 52 // 100))
    floor = (clamp8(red * 7 // 100), clamp8(green * 7 // 100), clamp8(blue * 12 // 100))
    cx, cy, radius = width * 0.74, height * 0.14, width * 0.62
    rows = []
    for y in range(height):
        depth = y / (height - 1)
        # A vertical falloff only: a real 2D vignette would cost a multiply per pixel.
        dim = 1.0 - 0.28 * max(0.0, depth - 0.62) / 0.38
        base = (
            clamp8(int((top[0] + (floor[0] - top[0]) * depth) * dim)),
            clamp8(int((top[1] + (floor[1] - top[1]) * depth) * dim)),
            clamp8(int((top[2] + (floor[2] - top[2]) * depth) * dim)),
        )
        dy2 = (y - cy) ** 2
        row = []
        for x in range(width):
            distance2 = (x - cx) ** 2 + dy2
            if distance2 > radius * radius:
                row.append(base)
                continue
            halo = (1.0 - (distance2**0.5 / radius)) * 0.45
            row.append(
                (
                    clamp8(int(base[0] + (red - base[0]) * halo)),
                    clamp8(int(base[1] + (green - base[1]) * halo)),
                    clamp8(int(base[2] + (blue - base[2]) * halo)),
                )
            )
        rows.append(row)
    return rows


def _sunset_rows(
    width: int, height: int, red: int, green: int, blue: int
) -> list[list[tuple[int, int, int]]]:
    """Night blue at the top, the accent in the middle, warm light at the horizon."""
    night = (12, 12, 34)
    embers = (255, 172, 92)
    rows = []
    for y in range(height):
        depth = y / (height - 1)
        sky = _blend(night, (red, green, blue), max(0.0, 1.0 - depth * 1.5))
        sky = _blend(sky, embers, depth**2.2)
        row = []
        for x in range(width):
            spread = max(0.0, 1.0 - abs(x / (width - 1) - 0.5) * 1.7) * max(0.0, 1.0 - depth * 1.25)
            row.append(_blend(sky, (255, 226, 178), spread * 0.45))
        rows.append(row)
    return rows


def _rays_rows(width: int, height: int, red: int, green: int, blue: int) -> list[list[tuple[int, int, int]]]:
    """Concentric bands rising from below the horizon, like a retro sunburst."""
    step = max(12, width // 40)
    rows = []
    for y in range(height):
        depth = y / (height - 1)
        dy2 = (y - height) ** 2
        row = []
        for x in range(width):
            band = int(((x - width / 2) ** 2 + dy2) ** 0.5 // step) % 2
            bright = 0.55 + 0.35 * band
            row.append(
                (
                    clamp8(int(red * bright * (1.0 - depth * 0.8))),
                    clamp8(int(green * bright * (1.0 - depth * 0.8))),
                    clamp8(int(blue * bright * (1.0 - depth * 0.8))),
                )
            )
        rows.append(row)
    return rows


def _grid_rows(width: int, height: int, red: int, green: int, blue: int) -> list[list[tuple[int, int, int]]]:
    """A lit wireframe horizon: the retro terminal look, brightest at the horizon."""
    cell = max(16, width // 40)
    night = (clamp8(red * 5 // 100), clamp8(green * 5 // 100), clamp8(blue * 8 // 100))
    horizon = int(height * 0.62)
    rows = []
    for y in range(height):
        depth = y / (height - 1)
        # Rows crowd together as they rise toward the horizon, like a floor in perspective.
        offset = int((y - horizon) * (0.35 if y < horizon else 1.0))
        on_row = (y - horizon) % cell == 0 or (offset + horizon) % max(1, cell // 3) == 0
        glow = max(0.0, 1.0 - abs(depth - 0.62) * 2.2)
        base = _blend(
            night, (clamp8(red * 40 // 100), clamp8(green * 40 // 100), clamp8(blue * 40 // 100)), glow
        )
        lit = _blend(base, (red, green, blue), 0.45 + 0.5 * glow)
        row = []
        for x in range(width):
            row.append(lit if on_row or x % cell == 0 else base)
        rows.append(row)
    return rows


def _waves_rows(width: int, height: int, red: int, green: int, blue: int) -> list[list[tuple[int, int, int]]]:
    """Layered crests in the accent colour, lit along the ridge."""
    dark = (clamp8(red * 8 // 100), clamp8(green * 8 // 100), clamp8(blue * 8 // 100))
    swell_span = max(1.0, width / 6.0)
    edge = max(1.5, height / 420)
    crest = (clamp8(red * 17 // 10), clamp8(green * 17 // 10), clamp8(blue * 17 // 10))
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            swell = height * (
                0.52
                + 0.14 * (triangle(x / swell_span) - 0.5)
                + 0.05 * (triangle(y / max(1.0, height / 3.0) + x / (swell_span * 1.5)) - 0.5)
            )
            near = abs(y - swell) / (height * 0.34)
            colour = _blend(dark, (red, green, blue), max(0.0, 1.0 - near))
            row.append(crest if near * height < edge * 1.6 else colour)
        rows.append(row)
    return rows


def triangle(value: float) -> float:
    """A triangle wave in 0..1 -- cheap, smooth enough, and no math import needed."""
    phase = value % 2.0
    return 1.0 - abs(phase - 1.0)


def write_png(path: Path, rows: Sequence[Sequence[tuple[int, int, int]]]) -> tuple[int, int]:
    """Write an 8-bit RGB PNG using only zlib and struct; returns the size written."""
    if not rows or not rows[0]:
        raise RecipeError("cannot write a PNG without any pixels")
    height, width = len(rows), len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter 0: each scanline is stored literally
        for red, green, blue in row[:width]:
            raw += bytes((clamp8(red), clamp8(green), clamp8(blue)))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    blob = b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
            chunk(b"IEND", b""),
        )
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    except OSError as exc:
        raise RecipeError(f"cannot write {path} ({exc})") from None
    return width, height


def clamp8(value: float) -> int:
    """Squeeze a channel into the 0-255 range an 8-bit PNG can store."""
    return max(0, min(255, round(value)))


def read_png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG header (used by the preview and the tests)."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError as exc:
        raise RecipeError(f"cannot read {path} ({exc})") from None
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RecipeError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def render_wallpaper_png(
    recipe: Recipe, path: Path, *, width: int = 1920, height: int = 1080
) -> tuple[int, int]:
    """The one place the wallpaper is drawn, so preview and shipped file cannot drift."""
    return write_png(path, wallpaper_pixels(recipe, width, height))


def os_release_text(recipe: Recipe) -> str:
    """Branding a live system shows in ``/etc/os-release``."""
    url = recipe.home_url.strip() or DEFAULT_HOME_URL
    tagline = recipe.tagline.strip() or "Built with OS Maker"
    values = {
        "PRETTY_NAME": recipe.pretty_name,
        "NAME": recipe.name.strip(),
        "VERSION": f"{recipe.release} ({tagline})",
        "VERSION_ID": recipe.version.strip() or "1.0",
        "ID": re.sub(r"[^a-z0-9]", "", recipe.resolved_hostname.lower()) or "osmaker",
        "ID_LIKE": "debian",
        "HOME_URL": url,
        "SUPPORT_URL": url,
        "BUG_REPORT_URL": f"{url}/issues" if url == DEFAULT_HOME_URL else url,
        "OS_MAKER_PRESET": recipe.preset,
        "OS_MAKER_ACCENT": recipe.accent,
    }
    if recipe.author.strip():
        values["OS_MAKER_MAINTAINER"] = recipe.author.strip()
    lines = []
    for key, value in values.items():
        # Debian's own convention: identifiers bare, everything else quoted.
        lines.append(f"{key}={value}" if key in _OS_RELEASE_BARE else f'{key}="{value}"')
    return "\n".join(lines) + "\n"


_OS_RELEASE_BARE = frozenset({"ID", "ID_LIKE", "OS_MAKER_PRESET"})


def boot_issue_text(recipe: Recipe) -> str:
    """``/etc/issue``: the banner printed above the login prompt on the text consoles."""
    return (
        f"\n  {recipe.name.strip()}\n"
        f"  {recipe.tagline.strip() or 'Built with OS Maker'}\n"
        f"  Debian {recipe.release} - {recipe.architecture} - press Enter and log in as 'user'\n\n"
    )


def branding_hook_text(recipe: Recipe) -> str:
    """A live-build chroot hook that finishes what ``--copy`` started.

    The copied files already sit at their final paths, so this hook does the two things
    a copy cannot: hand the machine its hostname, and tell the desktop where the
    wallpaper is -- otherwise the branding is a file nobody shows.  Every branch is
    guarded, because the same hook runs on a minimal image with no desktop in it.
    """
    lines = [
        "#!/bin/sh",
        "# Generated by os-builder: the parts of the branding a copy cannot do.",
        "set -eu",
        "",
        f"printf '%s\n' '{recipe.resolved_hostname}' > /etc/hostname",
        "",
    ]
    if recipe.with_wallpaper:
        lines += [
            "if [ -f /usr/share/backgrounds/os-maker.png ]; then",
            "    # GNOME reads its background through dconf.",
            "    if [ -d /etc/dconf/db/local.d ]; then",
            "        umask 022",
            "        cat > /etc/dconf/db/local.d/00-os-maker <<'DCONF'",
            "[org/gnome/desktop/background]",
            "picture-uri='file:///usr/share/backgrounds/os-maker.png'",
            f"picture-options='{recipe.picture_options}'",
            f"primary-color='{recipe.accent}'",
            "color-shading-type='solid'",
            "DCONF",
            "        if [ ! -f /etc/dconf/profile/user ]; then",
            "            mkdir -p /etc/dconf/profile",
            "            printf '%s\n' user system local > /etc/dconf/profile/user",
            "        fi",
            "        if command -v dconf > /dev/null 2>&1; then",
            "            dconf update || true",
            "        fi",
            "    fi",
            "    # The lighter desktops read an XML profile instead.",
            "    if [ -d /etc/xdg/xfce4 ]; then",
            "        mkdir -p /etc/xdg/xfce4/xfconf/xfce-perchannel-xml",
            "        cat > /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml <<'XFCE'",
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<channel name="xfce4-desktop" version="1.0">',
            '  <property name="backdrop" type="empty">',
            '    <property name="screen0" type="empty">',
            '      <property name="monitor0" type="empty">',
            '        <property name="workspace0" type="empty">',
            '          <property name="last-image" type="string" value="/usr/share/backgrounds/os-maker.png"/>',
            f'          <property name="image-style" type="int" value="{XFCE_IMAGE_STYLE.get(recipe.picture_options, 5)}"/>',
            "        </property>",
            "      </property>",
            "    </property>",
            "  </property>",
            "</channel>",
            "XFCE",
            "    fi",
            "fi",
            "",
        ]
    lines += _system_setup_lines(recipe)
    lines.append('echo "os-builder: branding installed"')
    return "\n".join(lines) + "\n"


def _system_setup_lines(recipe: Recipe) -> list[str]:
    """The machine-level settings the branding hook applies, one guarded block each.

    Every block tests for the directory or command it needs, because the same hook runs
    on a 200 MB rescue stick and on a full desktop: an unguarded ``locale-gen`` would
    fail the build for want of a package the image never promised.
    """
    lines: list[str] = []
    if recipe.timezone:
        zone = recipe.timezone.strip()
        lines += [
            "if [ -f /usr/share/zoneinfo/" + zone + " ]; then",
            "    ln -sf /usr/share/zoneinfo/" + zone + " /etc/localtime",
            # the timezone validator already restricts this to path characters
            f"    printf '%s\n' {zone} > /etc/timezone",
            "fi",
            "",
        ]
    if recipe.locale:
        locale = recipe.locale.strip()
        lines += [
            "if [ -f /etc/locale.gen ]; then",
            f"    grep -q '^{locale} ' /etc/locale.gen || printf '%s\n' '{locale} UTF-8' >> /etc/locale.gen",
            "    if command -v locale-gen > /dev/null 2>&1; then",
            "        locale-gen > /dev/null 2>&1 || true",
            "    fi",
            f"    printf '%s\n' LANG={sh_quote(locale)} > /etc/default/locale",
            "fi",
            "",
        ]
    if recipe.keyboard:
        keymap = recipe.keyboard.strip()
        lines += [
            "if [ -d /etc/default ]; then",
            "    cat > /etc/default/keyboard <<'XKB'",
            f'XKBLAYOUT="{keymap}"',
            'XKBMODEL="pc105"',
            'XKBVARIANT=""',
            'XKBOPTIONS=""',
            "XKB",
            "fi",
            "if [ -d /usr/lib/systemd ]; then",
            f"    printf '%s\n' KEYMAP={sh_quote(keymap)} > /etc/vconsole.conf",
            "fi",
            "",
        ]
    if recipe.apt_sources:
        lines += [
            "if [ -d /etc/apt ]; then",
            "    cat > /etc/apt/sources.list.d/os-maker.list <<'OSMAKER_SOURCES'",
        ]
        lines += [line.strip() for line in recipe.apt_sources if line.strip()]
        lines += ["OSMAKER_SOURCES", "fi", ""]
    if recipe.remove_packages:
        names = " ".join(sh_quote(item.strip()) for item in recipe.remove_packages if item.strip())
        lines += [
            "if command -v apt-get > /dev/null 2>&1; then",
            f"    apt-get purge -y --auto-remove {names} > /dev/null 2>&1 || true",
            "fi",
            "",
        ]
    if recipe.grub_timeout != 5:
        timeout = recipe.grub_timeout
        lines += [
            "if [ -f /etc/default/grub ]; then",
            f"    sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT={timeout}/' /etc/default/grub",
            f"    sed -i 's/^GRUB_TIMEOUT_STYLE=.*/GRUB_TIMEOUT_STYLE={('hidden' if timeout == 0 else 'menu')}/' /etc/default/grub",
            "    if command -v grub-mkconfig > /dev/null 2>&1; then",
            "        grub-mkconfig -o /boot/grub/grub.cfg > /dev/null 2>&1 || true",
            "    fi",
            "fi",
            "",
        ]
    return lines


def sh_quote(value: str) -> str:
    """Quote a single word for /bin/sh, given the validators already confined its alphabet."""
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class Branding:
    """What :func:`make_branding` produced."""

    copies: tuple[tuple[Path, str], ...]
    hook: Path
    wallpaper: Path | None
    record: Path | None = None
    size: tuple[int, int] = (0, 0)
    bytes: int = 0


def make_branding(recipe: Recipe, directory: Path) -> Branding:
    """Write the generated artwork and text into ``directory``.

    Files that belong at a fixed path travel as ``--copy`` arguments; the hook travels
    as ``--hook`` and does the rest.  Both halves go to build_os.py as ordinary flags,
    so the builder keeps ownership of how an image is assembled.
    """
    directory.mkdir(parents=True, exist_ok=True)
    size = parse_size(recipe.wallpaper_size) or (1920, 1080)
    wallpaper: Path | None = None
    if recipe.with_wallpaper:
        wallpaper = directory / "wallpaper.png"
        render_wallpaper_png(recipe, wallpaper, width=size[0], height=size[1])
    (directory / "motd").write_text(recipe.display_motd, encoding="utf-8")
    (directory / "issue").write_text(boot_issue_text(recipe), encoding="utf-8")
    (directory / "os-release").write_text(os_release_text(recipe), encoding="utf-8")
    record: Path | None = None
    if recipe.with_branding_record:
        # a machine-readable card in the image: what a guest can read to find out who
        # built it, and the one thing CI can grep to prove the branding arrived
        record = directory / "os-maker.json"
        record.write_text(recipe.branding_record, encoding="utf-8")
    hook = directory / "branding.hook.chroot"
    hook.write_text(branding_hook_text(recipe), encoding="utf-8")
    with contextlib.suppress(OSError):  # mode bits are a nicety; Windows has none
        os.chmod(hook, 0o755)
    copies: list[tuple[Path, str]] = [
        (directory / "motd", "/etc/motd"),
        (directory / "issue", "/etc/issue"),
        (directory / "os-release", "/etc/os-release"),
    ]
    if record is not None:
        copies.append((record, "/etc/os-maker.json"))
    if wallpaper is not None:
        copies.insert(0, (wallpaper, "/usr/share/backgrounds/os-maker.png"))
    used = [path for path, _ in copies] + [hook]
    return Branding(
        copies=tuple(copies),
        hook=hook,
        wallpaper=wallpaper,
        record=record,
        size=size,
        bytes=sum(path.stat().st_size for path in used if path.exists()),
    )


# --------------------------------------------------------------------------- #
# progress, artifacts and the runner (no Tk, so it is testable headlessly)
# --------------------------------------------------------------------------- #
STAGES: tuple[tuple[str, str, float], ...] = (
    ("installing live-build", "installing live-build", 0.18),
    ("lb config", "configuring live-build", 0.22),
    ("apt-get update", "refreshing the package index", 0.14),
    ("I: Configuration", "configuring the project", 0.26),
    ("I: Bootstrap", "bootstrapping Debian", 0.36),
    ("I: Chroot", "customising the chroot", 0.48),
    ("I: Bootloader", "installing the bootloader", 0.62),
    ("I: Binary", "assembling the live image", 0.7),
    ("squashfs", "compressing the filesystem", 0.78),
    ("I: ISO", "writing the ISO", 0.88),
    ("hybrid", "making the image bootable", 0.93),
    ("Collecting the image", "collecting the image", 0.97),
    ("Bootable ISO created", "done", 1.0),
)

WARNING_RE = re.compile(r"\bwarning\b|deprecat", re.IGNORECASE)
ERROR_RE = re.compile(r"^os-maker: |^\s*E: |failed|no such file", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
APT_PERCENT_RE = re.compile(r"[\[(](\d{1,3})%[\])]")


@dataclass(frozen=True)
class Progress:
    """How far along the build is, as far as its own output tells us."""

    label: str = "starting"
    fraction: float = 0.0
    lines: int = 0
    apt_percent: float | None = None
    done: bool = False
    ok: bool | None = None

    @property
    def text(self) -> str:
        if self.done:
            return self.label
        if self.apt_percent is not None and self.fraction < 0.3:
            return f"{self.label} - {self.apt_percent:.0f}%"
        return self.label


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line).replace("\r", "")


def progress_from_line(line: str, current: Progress) -> Progress:
    """Advance the estimate from one log line.

    live-build prints a predictable run of stage markers and apt prints percentages;
    those are better guesses than a bare spinner.  An unknown line keeps the last
    estimate, so the bar never jumps backwards.
    """
    text = line.strip()
    if not text:
        return current
    lowered = text.lower()
    fraction, label = current.fraction, current.label
    for marker, stage_label, weight in STAGES:
        if weight > fraction and marker.lower() in lowered:
            fraction, label = weight, stage_label
    if lowered.startswith(("get:", "fetched ")):
        fraction, label = max(fraction, 0.15), "downloading packages"
    match = APT_PERCENT_RE.search(text)
    apt_percent = float(match.group(1)) if match and fraction < 0.6 else None
    if text.startswith(("I:", "#")) and 0.26 < fraction < 0.97:
        fraction += 0.004  # quiet progress inside a long stage
    return replace(
        current, label=label, fraction=min(fraction, 0.97), lines=current.lines + 1, apt_percent=apt_percent
    )


@dataclass(frozen=True)
class Artifact:
    """One file the builder left behind."""

    path: Path
    kind: str
    size: int
    mtime: float

    @property
    def human_size(self) -> str:
        value = float(self.size)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GiB"

    @property
    def age(self) -> str:
        seconds = max(0.0, time.time() - self.mtime)
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{int(seconds // 60)} min ago"
        return time.strftime("%H:%M", time.localtime(self.mtime))


ARTIFACT_KINDS = {".iso": "disk image", ".sha256": "checksum", ".log": "build log"}


def list_artifacts(directory: Path, *, since: float = 0.0) -> list[Artifact]:
    """ISOs, checksums and logs in ``directory``; ``since`` limits them to one run."""
    found: list[Artifact] = []
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        if path.suffix not in ARTIFACT_KINDS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file() or (since and stat.st_mtime + 2 < since):
            continue
        found.append(
            Artifact(path=path, kind=ARTIFACT_KINDS[path.suffix], size=stat.st_size, mtime=stat.st_mtime)
        )
    found.sort(key=lambda artifact: artifact.mtime, reverse=True)
    return found


def verify_checksum(iso: Path) -> tuple[bool, str]:
    """Compare an ISO with its ``.sha256`` sidecar."""
    sidecar = Path(str(iso) + ".sha256")
    if not sidecar.is_file():
        return False, f"no checksum file next to {iso.name}"
    try:
        expected = next(
            (
                token
                for token in sidecar.read_text(encoding="utf-8").split()
                if re.fullmatch(r"[0-9a-f]{64}", token)
            ),
            "",
        )
    except OSError as exc:
        return False, f"cannot read {sidecar.name} ({exc})"
    if not expected:
        return False, f"{sidecar.name} holds no SHA-256 digest"
    digest = hashlib.sha256()
    try:
        with iso.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        return False, f"cannot read {iso.name} ({exc})"
    actual = digest.hexdigest()
    if actual == expected:
        return True, f"checksum matches ({actual[:16]}...)"
    return False, f"checksum mismatch: expected {expected[:16]}..., found {actual[:16]}..."


class BuildRun:
    """One builder invocation: the subprocess, its log as a stream, and the progress."""

    def __init__(self, argv: Sequence[str], *, cwd: Path | None = None) -> None:
        self.argv = [str(part) for part in argv]
        self.cwd = Path(cwd) if cwd else HERE
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.progress = Progress()
        self.started_at = 0.0
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.log: list[str] = []
        self.finish_code: int | None = None

    def start(self) -> None:
        if self.process is not None:
            raise RecipeError("this build has already been started")
        if not Path(self.argv[1]).is_file():
            raise RecipeError(f"cannot find the builder at {self.argv[1]}")
        self.started_at = time.time()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                self.argv,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RecipeError(f"cannot start the builder: {exc}") from None
        self.reader = threading.Thread(target=self._pump, name="os-builder-log", daemon=True)
        self.reader.start()

    def _pump(self) -> None:
        """Drain the child's output in the background; the GUI polls the queue."""
        process = self.process
        if process is None or process.stdout is None:
            return
        with process.stdout:
            for line in process.stdout:
                clean = strip_ansi(line).rstrip()
                self.log.append(clean)
                self.progress = progress_from_line(clean, self.progress)
                self.lines.put(clean)
        code = process.wait()
        self.finish_code = code
        self.progress = replace(
            self.progress,
            done=True,
            ok=code == 0,
            fraction=1.0 if code == 0 else self.progress.fraction,
            label="build finished" if code == 0 else f"build failed (exit {code})",
        )
        self.lines.put(None)

    def drain(self, limit: int = 400) -> list[str]:
        """Log lines that arrived since the last call, leaving the end marker in place."""
        batch: list[str] = []
        while len(batch) < limit:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                self.lines.put(None)
                break
            batch.append(line)
        return batch

    def stop(self) -> None:
        """Ask the builder to stop; a stubborn live-build is killed after a moment."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()

    @property
    def running(self) -> bool:
        return self.finish_code is None

    @property
    def elapsed(self) -> float:
        return 0.0 if not self.started_at else time.time() - self.started_at


def example_recipes() -> list[Path]:
    """Recipes shipped next to the scripts, sorted, missing folder tolerated.

    The GUI's "open an example" menu and ``--batch`` both need this list, and neither
    should fail because somebody cloned the repo without the examples directory.
    """
    folder = HERE / "examples"
    try:
        return sorted(path for path in folder.glob("*.json") if path.is_file())
    except OSError:
        return []


def revalidate(recipe: Recipe, changes: Mapping[str, object]) -> Recipe:
    """A copy of a recipe with some fields set, run back through the loader's checks.

    ``dataclasses.replace`` cannot express ``**dict[str, object]`` to a strict type
    checker -- but re-checking is the point: ``--fix`` must not be able to invent a
    recipe that would not have loaded in the first place.
    """
    if not changes:
        return recipe
    tidied = Recipe.from_dict({**asdict(recipe), **dict(changes)})
    tidied.base_dir = recipe.base_dir
    return tidied


def apply_environment(recipe: Recipe) -> Recipe:
    """Fill in the folders and container mode from the environment, when unset.

    ``OS_BUILDER_OUTPUT``, ``_WORK``, ``_CACHE``, ``_CONTAINER`` and ``_TIMEOUT`` let a
    build server or a container set the plumbing once, without editing recipes. Only
    empty fields are touched: a recipe that says something wins.
    """
    changes: dict[str, object] = {}
    for key, field_name in (
        ("OS_BUILDER_OUTPUT", "output_dir"),
        ("OS_BUILDER_WORK", "work_dir"),
        ("OS_BUILDER_CACHE", "cache_dir"),
        ("OS_BUILDER_CONTAINER", "container"),
    ):
        value = os.environ.get(key, "").strip()
        if value and not getattr(recipe, field_name):
            changes[field_name] = value
    timeout = os.environ.get("OS_BUILDER_TIMEOUT", "").strip()
    if timeout.isdigit() and 30 <= int(timeout) <= 7200:
        changes["boot_timeout"] = int(timeout)
    return revalidate(recipe, changes)


HINTS: tuple[tuple[str, str], ...] = (
    ("no space left on device", "the workspace filled up: point work_dir somewhere with 20 GiB or more"),
    (
        "cannot connect to the docker daemon",
        "Docker is not running for this user: start it, or run build_os.py where live-build is installed",
    ),
    (
        "permission denied",
        "a permission problem reading the workspace: if Docker is involved, the user usually needs the docker group",
    ),
    (
        "unable to locate package",
        "a package name is not in this release: check the spelling and the release field",
    ),
    (
        "failed to fetch",
        "an apt source could not be fetched: check the release, and any extra sources you added",
    ),
    ("no_pubkey", "an apt repository's signing key is missing; add it, or drop the source"),
    ("gpgv", "a repository signature could not be verified: an extra apt source may be unsigned"),
    ("boot test", "the boot test timed out: raise boot_timeout before blaming the image"),
)


def hint_from_log(lines: Iterable[str]) -> str:
    """One actionable sentence for the first known failure pattern in a log."""
    haystack = "\n".join(lines).lower()
    for needle, hint in HINTS:
        if needle in haystack:
            return hint
    return ""


def normalise(recipe: Recipe) -> tuple[Recipe, list[str]]:
    """The same recipe with its untidy parts tidied, and a note about each one.

    Used by ``--fix`` so a hand-edited JSON file can be made canonical without opening
    the GUI -- and so two recipes that mean the same thing can be compared at all.
    """
    changes: list[str] = []
    updates: dict[str, object] = {}
    for field_name in (
        "name",
        "tagline",
        "hostname",
        "release",
        "architecture",
        "preset",
        "compression",
        "container",
        "accent",
        "wallpaper_style",
        "wallpaper_size",
        "version",
        "author",
        "home_url",
        "locale",
        "keyboard",
        "timezone",
        "kernel",
        "picture_options",
    ):
        value = getattr(recipe, field_name)
        stripped = value.strip()
        if field_name == "accent":
            stripped = stripped.lower()
        if field_name == "wallpaper_size" and stripped:
            parsed = parse_size(stripped)
            if parsed is not None:
                stripped = f"{parsed[0]}x{parsed[1]}"
        if stripped != value:
            changes.append(f"{field_name}: {value!r} -> {stripped!r}")
            updates[field_name] = stripped
    for field_name in ("packages", "apt_sources", "remove_packages", "files", "hooks"):
        entries = getattr(recipe, field_name)
        tidied: list[str] = []
        for entry in entries:
            name = entry.strip()
            if name and name not in tidied:
                tidied.append(name)
        if tidied != entries:
            changes.append(f"{field_name}: {len(entries)} entries -> {len(tidied)}")
            updates[field_name] = tidied
    if recipe.motd.strip() and not recipe.motd.endswith("\n"):
        changes.append("motd: the trailing newline was missing")
        updates["motd"] = recipe.motd.rstrip("\n") + "\n"
    if recipe.hostname and recipe.hostname == hostname_for(recipe.name):
        changes.append("hostname: matches the name, so it can stay blank")
        updates["hostname"] = ""
    return revalidate(recipe, updates), changes


def diff_recipes(earlier: Recipe, later: Recipe) -> list[str]:
    """Field-level differences between two recipes, as lines a human can read."""
    lines: list[str] = []
    for name in Recipe.__dataclass_fields__:
        first, second = getattr(earlier, name), getattr(later, name)
        if first == second:
            continue
        if isinstance(first, list) or isinstance(second, list):
            before = set(first if isinstance(first, list) else [first])
            after = set(second if isinstance(second, list) else [second])
            added = ", ".join(sorted(after - before))
            removed = ", ".join(sorted(before - after))
            detail = ", ".join(
                part for part in (f"+{added}" if added else "", f"-{removed}" if removed else "") if part
            )
            lines.append(f"{name}: {detail or '(reordered)'}")
        else:
            lines.append(f"{name}: {first!r} -> {second!r}")
    return lines


# fields mapped to what they become: documentation the code can be checked against
# The left column of ``--explain``; long names would run into the description, so the
# table is printed at this width and a test keeps every name inside it.
EXPLAIN_NAME_WIDTH = 42

EXPLANATIONS: tuple[tuple[str, str], ...] = (
    ("name", "--name, the ISO label, and the hostname when that is blank"),
    ("tagline", "the login banner, /etc/issue and os-release's VERSION"),
    ("hostname", "/etc/hostname, written by the branding hook"),
    ("motd", "/etc/motd, verbatim, with the block name above it when motd_art is on"),
    ("release / architecture / compression", "--release, --architecture, --compression"),
    ("preset", "the starting package list; nothing on the command line itself"),
    ("packages", "--packages, after the preset's own and the chosen kernel"),
    ("files", "--copy SOURCE:DEST, resolved against this recipe's folder"),
    ("hooks", "--hook, before the generated branding hook"),
    ("kernel", "a linux-image-* package added to the list ('auto' lets Debian choose)"),
    ("apt_sources", "/etc/apt/sources.list.d/os-maker.list, from the branding hook"),
    ("remove_packages", "apt-get purge --auto-remove in the chroot hook"),
    ("locale / keyboard / timezone", "/etc/default/locale, /etc/default/keyboard, /etc/localtime"),
    ("grub_timeout", "GRUB_TIMEOUT in /etc/default/grub; 0 hides the menu"),
    ("version / author / home_url", "PRETTY_NAME, VERSION_ID, OS_MAKER_MAINTAINER, HOME_URL"),
    ("accent / wallpaper_style / wallpaper_size", "the generated PNG and os-release's OS_MAKER_ACCENT"),
    ("wallpaper_seed", "which variation of an organic style is drawn; same seed, same picture"),
    ("show_wordmark", "whether the name is stamped onto the wallpaper at all"),
    ("picture_options", "the desktop's background fit, in dconf and in XFCE's XML"),
    ("with_branding_record", "/etc/os-maker.json, carrying this recipe's digest"),
    ("with_firmware / with_wallpaper", "--with-firmware, and whether the PNG is generated"),
    ("container / boot_test / boot_timeout", "--container, --boot-test, --boot-timeout"),
    ("keep_work / skip_checks", "--keep-work, --skip-checks"),
    ("work_dir / cache_dir", "--work-dir, --cache-dir; both next to the ISO when blank"),
    ("output_dir", "where the ISO lands, default dist/ (OS_BUILDER_OUTPUT can set it)"),
    ("notes", "nothing: recipe-only, for whoever reads it next"),
)


def engine_hint() -> str:
    """One line about whether this machine can actually build right now."""
    if shutil.which("lb") and platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
        return "live-build is installed and you are root, so a native build is possible"
    if shutil.which("docker"):
        return "Docker was found, so the build will run inside a Debian container"
    return "no Docker and no live-build here: previews work, real builds will not"


def save_preview_png(recipe: Recipe, path: Path, *, width: int = 640, height: int = 360) -> Path:
    """Render only the wallpaper -- what ``--wallpaper`` and the GUI preview use."""
    render_wallpaper_png(recipe, path, width=width, height=height)
    return path


# --------------------------------------------------------------------------- #
# the model the GUI drives: no Tk in here, so the whole app is testable headlessly
# --------------------------------------------------------------------------- #
class RecipeModel:
    """Mutable recipe state plus the actions the builder offers.

    The GUI is a thin view over this class: widgets write into it, it notifies
    listeners, and nothing here needs a display.  That is also what makes the buttons
    testable, and it means the terminal modes do exactly the same thing the GUI does.
    """

    def __init__(self, recipe: Recipe | None = None, *, base_dir: Path | None = None) -> None:
        self.recipe = recipe if recipe is not None else Recipe()
        self.base_dir = Path(base_dir) if base_dir else HERE
        self.run: BuildRun | None = None
        self.scratch: Path | None = None
        self.path: Path | None = None
        self.listeners: list[Callable[[], None]] = []
        self.started_at = 0.0

    # -- plumbing ----------------------------------------------------------- #
    def subscribe(self, listener: Callable[[], None]) -> None:
        self.listeners.append(listener)

    def notify(self) -> None:
        for listener in self.listeners:
            listener()

    def set(self, **fields: object) -> None:
        """Set recipe fields by name, then tell the view."""
        for key, value in fields.items():
            if key not in Recipe.__dataclass_fields__:
                raise RecipeError(f"recipes have no {key!r} field")
            setattr(self.recipe, key, value)
        self.notify()

    def set_preset(self, key: str, *, keep_appearance: bool = False) -> None:
        """Adopt a preset's packages and defaults without losing the OS's name."""
        preset = PRESETS.get(key)
        if preset is None:
            raise RecipeError(f"no preset called {key!r}")
        updates: dict[str, object] = {
            "preset": key,
            "packages": [],
            "with_firmware": preset.firmware,
        }
        if not keep_appearance:
            updates.update(accent=preset.accent, wallpaper_style=preset.wallpaper)
        if not self.recipe.name.strip() or self.recipe.name in {p.label for p in PRESETS.values()}:
            updates["name"] = preset.label
            updates["tagline"] = preset.blurb
            updates["hostname"] = hostname_for(preset.label)
            updates["motd"] = default_motd(preset.label, preset.blurb)
        self.set(**updates)

    def toggle_package(self, name: str, *, on: bool | None = None) -> bool:
        """Add or drop one package from the user's own list; returns the new state."""
        wanted = name not in self.recipe.packages if on is None else on
        if wanted and name not in self.recipe.packages:
            self.recipe.packages = [*self.recipe.packages, name]
        elif not wanted and name in self.recipe.packages:
            self.recipe.packages = [item for item in self.recipe.packages if item != name]
        self.notify()
        return name in self.recipe.packages

    def add_packages(self, text: str) -> list[str]:
        """Add a comma- or space-separated batch; returns what was new."""
        added = [name for name in parse_package_list(text) if name not in self.recipe.packages]
        if added:
            self.recipe.packages = [*self.recipe.packages, *added]
            self.notify()
        return added

    def add_file(self, source: str | Path, destination: str) -> str:
        """Queue a host file to land at ``destination`` inside the image."""
        entry = f"{Path(str(source)).as_posix()}:{str(destination).strip() or '/root/'}"
        if entry not in self.recipe.files:
            self.recipe.files = [*self.recipe.files, entry]
            self.notify()
        return entry

    def remove_file(self, index: int) -> None:
        if 0 <= index < len(self.recipe.files):
            self.recipe.files = [item for i, item in enumerate(self.recipe.files) if i != index]
            self.notify()

    def add_hook(self, path: str | Path) -> str:
        entry = str(Path(str(path)))
        if entry not in self.recipe.hooks:
            self.recipe.hooks = [*self.recipe.hooks, entry]
            self.notify()
        return entry

    def remove_hook(self, index: int) -> None:
        if 0 <= index < len(self.recipe.hooks):
            self.recipe.hooks = [item for i, item in enumerate(self.recipe.hooks) if i != index]
            self.notify()

    # -- reading ------------------------------------------------------------ #
    @property
    def suggestions(self) -> list[str]:
        """Every package the presets know about, plus what this recipe already uses."""
        seen: dict[str, None] = {}
        for preset in PRESETS.values():
            for name in preset.packages:
                seen.setdefault(name, None)
        for name in self.recipe.packages:
            seen.setdefault(name, None)
        return sorted(seen)

    def problems(self) -> list[str]:
        return self.recipe.problems()

    def ready(self) -> bool:
        return self.recipe.ready()

    def preview_image(self, directory: Path, *, width: int = 384, height: int = 216) -> Path:
        """Render the wallpaper at preview size; returns the file to display."""
        return save_preview_png(self.recipe, directory / "preview.png", width=width, height=height)

    def branding_dir(self) -> Path:
        """Where generated artwork lives for this session (made once, reused)."""
        if self.scratch is None:
            self.scratch = Path(tempfile.mkdtemp(prefix="os-builder-"))
        return self.scratch / "branding"

    def close(self) -> None:
        if self.run is not None:
            self.run.stop()
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None

    # -- running ------------------------------------------------------------ #
    def argv(self, *, dry_run: bool = False) -> list[str]:
        """The build_os.py command line for the current recipe, branding included."""
        branding = make_branding(self.recipe, self.branding_dir())
        return recipe_to_argv(self.recipe, branding=branding.copies, hook=branding.hook, dry_run=dry_run)

    def preview(self, *, timeout: int = 180) -> tuple[int, str]:
        """Ask the real builder what it would do, without building anything."""
        return plan_summary(self.argv(dry_run=True), timeout=timeout)

    def start_build(self, *, dry_run: bool = False) -> BuildRun:
        if self.run is not None and self.run.running:
            raise RecipeError("a build is already running -- stop it first")
        problems = self.problems()
        if problems and not dry_run:
            raise RecipeError("; ".join(problems))
        run = BuildRun(self.argv(dry_run=dry_run), cwd=self.base_dir)
        self.run = run
        self.started_at = run.started_at or time.time()
        run.start()
        self.started_at = run.started_at
        return run

    def artifacts(self) -> list[Artifact]:
        return list_artifacts(self.recipe.resolved_output_dir, since=self.started_at)

    def save(self, path: Path) -> Path:
        self.recipe.save(path)
        self.path = path
        return path

    def open(self, path: Path) -> Recipe:
        self.recipe = Recipe.from_file(path)
        self.path = path
        self.notify()
        return self.recipe


# --------------------------------------------------------------------------- #
# command line (the GUI is in os_builder_gui.py, imported only when wanted)
# --------------------------------------------------------------------------- #
def load_recipe(path: Path | None, *, preset: str | None = None) -> Recipe:
    """The recipe to start the GUI with: a file, a preset, or the defaults.

    Environment defaults are applied here too, so the GUI and --build agree about what
    a recipe means: a folder set in the shell must not change the build but not the
    window's own account of it.
    """
    if path is not None:
        return apply_environment(Recipe.from_file(path))
    if preset is not None:
        return apply_environment(Recipe.for_preset(preset))
    return apply_environment(Recipe())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="os_builder.py",
        description="Build fantastic Debian live ISOs from a recipe, with a GUI.",
        epilog=(
            "With no arguments the GUI starts. The headless modes do exactly what the\n"
            "buttons do, so a recipe is reproducible from a terminal or in CI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-r", "--recipe", type=Path, metavar="FILE", help="recipe to open in the GUI")
    parser.add_argument("--preset", choices=tuple(sorted(PRESETS)), help="start from a preset")
    parser.add_argument("--list-presets", action="store_true", help="print the presets and exit")
    parser.add_argument(
        "--check",
        type=Path,
        nargs="+",
        metavar="FILE",
        help="validate one or more recipes (exit 0 means all are fine)",
    )
    parser.add_argument(
        "--preview", type=Path, metavar="FILE", help="print the builder argv and its dry-run plan"
    )
    parser.add_argument("--build", type=Path, metavar="FILE", help="run the build with no GUI")
    parser.add_argument("--wallpaper", type=Path, metavar="FILE", help="write the recipe's wallpaper PNG")
    parser.add_argument("--out", type=Path, metavar="FILE", help="where --new/--wallpaper write")
    parser.add_argument("--new", action="store_true", help="write a starter recipe to --out")
    parser.add_argument(
        "--timeout", type=int, default=7200, metavar="SECONDS", help="--build limit (default 2h)"
    )
    parser.add_argument("--keep-work", action="store_true", help="with --build: keep the workspace")

    machine = parser.add_argument_group("for scripts")
    machine.add_argument(
        "--json", action="store_true", help="with --check/--preview/--build: one JSON object on stdout"
    )
    machine.add_argument("--quiet", action="store_true", help="with --build: do not echo the builder's log")
    machine.add_argument(
        "--tail",
        type=int,
        default=0,
        metavar="N",
        help="with --build --quiet: repeat the last N log lines when the build fails",
    )
    machine.add_argument(
        "--stall-notice",
        type=int,
        default=0,
        metavar="SECONDS",
        help="with --build: say so when the builder has printed nothing for this long",
    )

    tools = parser.add_argument_group("recipe tools")
    tools.add_argument("--fix", type=Path, metavar="FILE", help="print the canonical form of a recipe")
    tools.add_argument(
        "--diff", type=Path, nargs=2, metavar=("A", "B"), help="print what differs between two recipes"
    )
    tools.add_argument("--batch", type=Path, metavar="DIR", help="check every recipe in a directory")
    tools.add_argument("--preset-info", metavar="KEY", help="one preset in full")
    tools.add_argument("--explain", action="store_true", help="what each recipe field turns into")
    tools.add_argument(
        "--gui-check", action="store_true", help="report whether this machine can show the GUI"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.explain:
        for name, meaning in EXPLANATIONS:
            print(f"  {name:<{EXPLAIN_NAME_WIDTH}}{meaning}")
        return 0

    if args.gui_check:
        return gui_check()

    if args.preset_info:
        return preset_info(args.preset_info)

    if args.diff is not None:
        return diff_recipes_and_report(args.diff[0], args.diff[1])

    if args.batch is not None:
        return batch_check(args.batch)

    if args.list_presets:
        if args.json:
            print(json.dumps([asdict(preset) for preset in PRESETS.values()], indent=2))
            return 0
        for key, preset in PRESETS.items():
            print(f"{key:<12} {preset.label}")
            print(f"{'':<12}{preset.blurb}")
            print(f"{'':<12}packages: {', '.join(preset.packages)}")
        return 0

    if args.new:
        recipe = Recipe.for_preset(args.preset) if args.preset else Recipe()
        destination = args.out or Path("fantastic-os.json")
        recipe.save(destination)
        print(f"os-builder: wrote {destination}  (edit it, then: os_builder.py --preview {destination})")
        return 0

    if args.fix is not None:
        return fix_recipe(args.fix, args.out)

    if args.check is not None:
        return max((check_recipe(path, as_json=args.json) for path in args.check), default=0)

    if args.wallpaper is not None:
        return write_wallpaper(args.wallpaper, args.out)

    if args.preview is not None:
        return preview(args.preview, as_json=args.json)

    if args.build is not None:
        return headless_build(args)

    return launch_gui(args.recipe, args.preset)


def load_or_report(path: Path) -> Recipe | int:
    try:
        return apply_environment(Recipe.from_file(path))
    except RecipeError as exc:
        print(f"os-builder: {exc}", file=sys.stderr)
        return 1


def recipe_report(recipe: Recipe, path: Path | None = None, *, dry_run: bool = False) -> dict[str, object]:
    """Everything ``--json`` says about one recipe.

    ``--preview`` passes ``dry_run`` so the command it hands to a script is the command
    that would actually run, ``--dry-run`` and all.
    """
    return {
        "file": str(path) if path is not None else "",
        "digest": recipe.digest(),
        "ready": not recipe.problems(),
        "problems": recipe.problems(),
        "release": recipe.release,
        "architecture": recipe.architecture,
        "packages": recipe.merged_packages(),
        "wallpaper_size": recipe.wallpaper_size,
        "hostname": recipe.resolved_hostname,
        "command": recipe_to_argv(recipe, dry_run=dry_run),
    }


def fix_recipe(path: Path, out: Path | None) -> int:
    """Print (and optionally write) the canonical form of a recipe."""
    loaded = load_or_report(path)
    if isinstance(loaded, int):
        return loaded
    tidied, changes = normalise(loaded)
    for change in changes:
        print(f"os-builder: {change}")
    problems = tidied.problems()
    if not changes:
        print("os-builder: nothing to tidy")
    if out is None:
        print(tidied.to_json(), end="")
        if changes:
            print("os-builder: pass --out FILE to keep this, the file itself was not touched")
    else:
        try:
            tidied.save(out)
        except RecipeError as exc:
            print(f"os-builder: {exc}", file=sys.stderr)
            return 2
        print(f"os-builder: wrote {out} ({len(changes)} change(s))")
    for problem in problems:
        print(f"os-builder: still not buildable: {problem}", file=sys.stderr)
    return 2 if problems else 0


def diff_recipes_and_report(first: Path, second: Path) -> int:
    left = load_or_report(first)
    if isinstance(left, int):
        return left
    right = load_or_report(second)
    if isinstance(right, int):
        return right
    lines = diff_recipes(left, right)
    if not lines:
        print(f"os-builder: {first.name} and {second.name} describe the same build")
        return 0
    print(f"os-builder: {first.name} -> {second.name}")
    for line in lines:
        print(f"  {line}")
    return 1


def batch_check(folder: Path) -> int:
    """Check every recipe in a directory: the shape of a nightly image job."""
    if not folder.is_dir():
        print(f"os-builder: {folder} is not a directory", file=sys.stderr)
        return 2
    recipes = sorted(folder.glob("*.json"))
    if not recipes:
        print(f"os-builder: no recipes (*.json) in {folder}", file=sys.stderr)
        return 2
    worst = 0
    for path in recipes:
        worst = max(worst, check_recipe(path))
    print(f"os-builder: checked {len(recipes)} recipe(s) in {folder}")
    return worst


def preset_info(key: str) -> int:
    preset = PRESETS.get(key)
    if preset is None:
        print(f"os-builder: no preset called {key!r} (try: {', '.join(PRESETS)})", file=sys.stderr)
        return 2
    print(f"{preset.key}: {preset.label}")
    print(f"  {preset.blurb}")
    print(f"  packages: {', '.join(preset.packages) or 'none'}")
    print(f"  desktop task: {preset.desktop or 'no desktop'}")
    print(f"  firmware on by default: {'yes' if preset.firmware else 'no'}")
    print(f"  accent {preset.accent}, wallpaper style {preset.wallpaper}")
    return 0


def gui_check() -> int:
    """Say whether this machine can show the GUI, and why not if it cannot."""
    print(f"os-builder: {__version__}, python {platform.python_version()}, {platform.system()}")
    try:
        import tkinter
    except ImportError:
        print("  no tkinter: install python3-tk (Debian/Ubuntu), or use --check/--preview/--build")
        return 1
    print(f"  tkinter {tkinter.TkVersion}.{tkinter.TclVersion if hasattr(tkinter, 'TclVersion') else '?'}")
    try:
        probe = tkinter.Tk()
    except Exception as exc:  # TclError on a headless box, and worth naming either way
        print(f"  cannot open a display ({exc}): use --preview and --build, or run under a desktop")
        return 1
    try:
        probe.update_idletasks()
        print(f"  display ok, {int(probe.winfo_screenwidth())}x{int(probe.winfo_screenheight())} screen")
        try:
            scaling = float(probe.call("tk", "scaling"))
            print(f"  {scaling:.2f} pixels per point; the preview adapts to this")
        except Exception:
            print("  could not read the display scaling; the preview uses its default size")
    finally:
        probe.destroy()
    return 0


def check_recipe(path: Path, *, as_json: bool = False) -> int:
    """Validate one recipe file, printing every reason it would not build."""
    recipe = load_or_report(path)
    if isinstance(recipe, int):
        return recipe
    problems = recipe.problems()
    if as_json:
        print(json.dumps(recipe_report(recipe, path), indent=2, sort_keys=True))
        return 0 if not problems else 2
    if not problems:
        packages = len(recipe.merged_packages())
        print(
            f"os-builder: {path.name} looks good ({packages} packages, {recipe.release}, {recipe.wallpaper_size})"
        )
        return 0
    for problem in problems:
        print(f"os-builder: {path.name}: {problem}", file=sys.stderr)
    return 2


def write_wallpaper(path: Path, out: Path | None) -> int:
    loaded = load_or_report(path)
    if isinstance(loaded, int):
        return loaded
    size = parse_size(loaded.wallpaper_size) or (640, 360)
    destination = out or Path(f"{loaded.resolved_hostname}-wallpaper.png")
    render_wallpaper_png(loaded, destination, width=size[0], height=size[1])
    written = read_png_size(destination)
    print(f"os-builder: wrote {destination} ({written[0]}x{written[1]}, {destination.stat().st_size} bytes)")
    return 0


def preview(path: Path, *, as_json: bool = False) -> int:
    loaded = load_or_report(path)
    if isinstance(loaded, int):
        return loaded
    if as_json:
        # no child process here: a script wants the command line, not a wall of text
        print(json.dumps(recipe_report(loaded, path, dry_run=True), indent=2, sort_keys=True))
        return 0 if not loaded.problems() else 2
    for problem in loaded.problems():
        print(f"os-builder: warning: {problem}", file=sys.stderr)
    scratch = Path(tempfile.mkdtemp(prefix="os-builder-"))
    try:
        branding = make_branding(loaded, scratch / "branding")
        argv = recipe_to_argv(loaded, branding=branding.copies, hook=branding.hook, dry_run=True)
        print(describe_argv(argv))
        print()
        code, text = plan_summary(argv)
        print(text)
        print()
        print(f"os-builder: generated {len(branding.copies)} branded files ({branding.bytes} bytes)")
        return code
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def headless_build(args: argparse.Namespace) -> int:
    """Run one build straight from a recipe: same pipeline, same flags, no Tk."""
    loaded = load_or_report(args.build)
    if isinstance(loaded, int):
        return loaded
    problems = loaded.problems()
    if problems:
        for problem in problems:
            print(f"os-builder: {problem}", file=sys.stderr)
        return 2
    recipe = replace(loaded, keep_work=loaded.keep_work or args.keep_work)
    scratch = Path(tempfile.mkdtemp(prefix="os-builder-"))
    run: BuildRun | None = None
    status = 1
    # getattr, not args.x: a caller assembling a Namespace by hand (a test, another
    # tool) should not have to know about every flag this function happens to like
    quiet = bool(getattr(args, "quiet", False))
    tail: deque[str] = deque(maxlen=max(0, int(getattr(args, "tail", 0))))
    stall_after = max(0, int(getattr(args, "stall_notice", 0)))
    started = time.monotonic()
    deadline = started + max(1, args.timeout)
    last_output = started
    warned_stall = False
    artifacts: list[Artifact] = []
    try:
        try:
            branding = make_branding(recipe, scratch / "branding")
            run = BuildRun(recipe_to_argv(recipe, branding=branding.copies, hook=branding.hook), cwd=HERE)
            if not quiet:
                print(f"os-builder: {describe_argv(run.argv)}", flush=True)
                print(f"os-builder: {engine_hint()}", flush=True)
            run.start()
        except RecipeError as exc:
            print(f"os-builder: {exc}", file=sys.stderr)
            return 2
        while run.running:
            if echo_lines(run, echo=not quiet, sink=tail):
                last_output = time.monotonic()
                warned_stall = False
            elif stall_after and not warned_stall and time.monotonic() - last_output > stall_after:
                # a build that prints nothing for minutes is usually fetching a package
                # index or waiting on a lock; say so, rather than looking hung
                print(
                    f"os-builder: still running, nothing printed for {stall_after}s ({run.progress.label})",
                    file=sys.stderr,
                    flush=True,
                )
                warned_stall = True
            if time.monotonic() > deadline:
                print("os-builder: timed out; asking the builder to stop", file=sys.stderr)
                status = 1
                break
            time.sleep(0.05)
        else:
            status = run.finish_code if run.finish_code is not None else 1
    except KeyboardInterrupt:
        print("\nos-builder: interrupted", file=sys.stderr)
        status = 130
    finally:
        if run is not None:
            if run.running:
                run.stop()
            echo_lines(run, echo=not quiet, sink=tail)
            artifacts = list_artifacts(recipe.resolved_output_dir, since=run.started_at)
        shutil.rmtree(scratch, ignore_errors=True)
        if status != 0:
            for line in list(tail) if quiet else ():
                print(line)
            hint = hint_from_log(run.log if run is not None else ())
            if hint:
                print(f"os-builder: hint: {hint}", file=sys.stderr)
        if getattr(args, "json", False):
            summary = {
                "status": status,
                "seconds": round(time.monotonic() - started, 1),
                "digest": recipe.digest(),
                "command": list(run.argv) if run is not None else [],
                "artifacts": [
                    {"kind": item.kind, "path": str(item.path), "size": item.size} for item in artifacts
                ],
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
        for artifact in artifacts:
            print(f"os-builder: {artifact.kind}: {artifact.path} ({artifact.human_size})")
    return status


def echo_lines(run: BuildRun, *, echo: bool = True, sink: deque[str] | None = None) -> int:
    """Drain the builder's log: print it, keep the tail, or both.

    Draining is not optional even when ``echo`` is off -- the child's stdout blocks
    once the pipe fills, so a quiet build that never reads would hang the build.
    """
    lines = run.drain()
    for line in lines:
        if sink is not None:
            sink.append(line)
        if echo:
            print(line, flush=True)
    return len(lines)


def launch_gui(recipe_path: Path | None = None, preset: str | None = None) -> int:
    """Start the Tk interface, or explain precisely why it cannot start."""
    try:
        import tkinter  # noqa: F401  (the real check: is there a Tk at all?)
    except ImportError:
        print(
            "os-builder: this Python has no Tkinter, so the GUI cannot start.\n"
            "            Install it (Debian/Ubuntu: sudo apt install python3-tk; macOS: brew install\n"
            "            python-tk; Windows: reinstall Python with 'tcl/tk and IDLE' ticked), or stay\n"
            "            in the terminal -- the same recipe works there:\n"
            "\n"
            "            python3 os_builder.py --new --out my.json\n"
            "            python3 os_builder.py --preview my.json\n"
            "            python3 os_builder.py --build my.json\n",
            file=sys.stderr,
        )
        return 3
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        from os_builder_gui import BuilderGUI
    except ImportError as exc:  # pragma: no cover - a broken install, not a code path
        print(f"os-builder: the GUI module could not be loaded ({exc})", file=sys.stderr)
        return 3
    try:
        recipe = load_recipe(recipe_path, preset=preset)
    except RecipeError as exc:
        print(f"os-builder: {exc}", file=sys.stderr)
        return 2
    BuilderGUI(recipe).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
