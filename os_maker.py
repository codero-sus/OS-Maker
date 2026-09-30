#!/usr/bin/env python3
"""OS Maker: a tiny, safe, persistent operating-system simulator.

A miniature shell and in-memory filesystem in pure Python -- no root, no VM, no
network.  Everything lives in a JSON "virtual disk" next to this file, so your
files survive between runs.  This is the playful counterpart of ``build_os.py``,
which produces a real bootable Debian ISO; ``os_maker.py`` only simulates one.

Usage:
    python3 os_maker.py                        # interactive shell
    python3 os_maker.py -e "ls -l /" -e "date" # run commands and quit (scriptable)
    python3 os_maker.py --reset                # start from a fresh filesystem
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import itertools
import json
import os
import platform
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "2.0.0"
DEFAULT_SAVE_FILE = Path(__file__).with_name("os_maker_data.json")
HOME = "/home/guest"
USER = "guest"
BLOCK_SIZE = 1024
DISK_BLOCKS = 4 * 1024 * 1024  # a 4 GiB "virtual disk"
MAX_NAME_LENGTH = 255
VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")
UNSUPPORTED_OPERATORS = "<>|;&"


# --------------------------------------------------------------------------- #
# filesystem
# --------------------------------------------------------------------------- #
def make_dir(mtime: int | None = None) -> dict[str, Any]:
    return {"type": "dir", "children": {}, "mtime": mtime if mtime is not None else now()}


def make_file(content: str = "", mtime: int | None = None) -> dict[str, Any]:
    return {"type": "file", "content": content, "mtime": mtime if mtime is not None else now()}


def now() -> int:
    return int(time.time())


SEED_DIRECTORIES = (
    "/bin",
    "/boot",
    "/etc",
    "/home",
    "/home/guest",
    "/proc",
    "/tmp",
    "/usr",
    "/usr/share",
    "/var",
    "/var/log",
)
SEED_FILES: dict[str, str] = {
    "/etc/os-release": (
        'PRETTY_NAME="OS Maker 2.0"\nNAME="OS Maker"\nVERSION_ID="2.0"\nID=osmaker\nSUPPORT_URL="https://example.invalid/os-maker"\n'
    ),
    "/etc/hostname": "osmaker\n",
    "/etc/motd": (
        "Welcome to OS Maker, a shell that runs entirely in memory.\n"
        "Nothing here can touch your real files. Type 'help' for the command list,\n"
        "'man ls' for one command, 'tree /' to look around and 'exit' to shut down.\n"
    ),
    "/var/log/boot.log": "[    0.000000] osmaker: virtual disk mounted\n[    0.000100] osmaker: shell ready\n",
    "/home/guest/welcome.txt": "Try: mkdir -p projects/idea, append projects/idea.txt Ship it, tree ~\n",
    "/home/guest/notes.txt": "OS Maker keeps its disk in os_maker_data.json next to os_maker.py\n",
    "/usr/share/hello.txt": "hello from the mini OS\n",
}


def ensure_path(root: dict[str, Any], path: str) -> dict[str, Any]:
    """Create every directory along ``path`` (like ``mkdir -p``) and return the last one."""
    node = root
    for part in PurePosixPath(path).parts:
        if part in ("/", ""):
            continue
        node = node["children"].setdefault(part, make_dir())
        if not is_dir(node):  # pragma: no cover - only for hand-edited disks
            raise OSError(f"{path}: a file is in the way")
    return node


def fresh_fs() -> dict[str, Any]:
    """The filesystem a brand new install boots with."""
    root = make_dir()
    for directory in SEED_DIRECTORIES:
        ensure_path(root, directory)
    for path, content in SEED_FILES.items():
        parent = ensure_path(root, str(PurePosixPath(path).parent))
        parent["children"][PurePosixPath(path).name] = make_file(content)
    return root


def validate_fs(node: Any, path: str = "/") -> None:
    """Recursively check the shape of a saved filesystem before trusting it."""
    if not isinstance(node, dict):
        raise ValueError(f"{path}: expected an object, found {type(node).__name__}")
    kind = node.get("type")
    if kind == "dir":
        children = node.get("children")
        if not isinstance(children, dict):
            raise ValueError(f"{path}: directory without a children map")
        for name, child in children.items():
            if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
                raise ValueError(f"{path}: invalid entry name {name!r}")
            if len(name.encode("utf-8")) > MAX_NAME_LENGTH:
                raise ValueError(f"{path}: entry name too long")
            validate_fs(child, f"{path.rstrip('/')}/{name}")
    elif kind == "file":
        if not isinstance(node.get("content", ""), str):
            raise ValueError(f"{path}: file content must be a string")
    else:
        raise ValueError(f"{path}: unknown node type {kind!r}")


def is_dir(node: Any) -> bool:
    """True for a directory node (the two node kinds are the whole data model)."""
    return isinstance(node, dict) and node.get("type") == "dir"


def is_file(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "file"


def children_of(node: Any) -> dict[str, Any]:
    """The entries of a directory node (empty for anything else)."""
    if not isinstance(node, dict):
        return {}
    children = node.get("children")
    return children if isinstance(children, dict) else {}


def mtime_of(node: Any, fallback: int) -> int:
    stamp = node.get("mtime") if isinstance(node, dict) else None
    return stamp if isinstance(stamp, int) else fallback


def node_size(node: dict[str, Any]) -> int:
    if not is_file(node):
        return 4096
    return len(node.get("content", "").encode("utf-8"))


def content_size(node: dict[str, Any]) -> int:
    if is_file(node):
        return len(node.get("content", "").encode("utf-8"))
    return sum(content_size(child) for child in children_of(node).values())


def node_blocks(node: dict[str, Any]) -> int:
    """Blocks used by a node; every file takes at least one, like a real filesystem."""
    if not is_file(node):
        return sum(node_blocks(child) for child in children_of(node).values())
    size = len(node.get("content", "").encode("utf-8"))
    return max(1, -(-size // BLOCK_SIZE))


def link_count(node: dict[str, Any]) -> int:
    if not is_dir(node):
        return 1
    return 2 + sum(1 for child in children_of(node).values() if is_dir(child))


def format_time(stamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


# --------------------------------------------------------------------------- #
# command line lexing
# --------------------------------------------------------------------------- #
class ShellSyntaxError(ValueError):
    """A malformed command line (unbalanced quote, unsupported operator, ...)."""


@dataclass(frozen=True)
class Token:
    """One word of the command line, or a redirection operator."""

    value: str
    operator: bool = False
    quoted: bool = False


@dataclass(frozen=True)
class ParsedLine:
    command: str
    args: list[str]
    redirect: str | None = None  # ">" or ">>"
    target: str | None = None


def lex(line: str) -> list[Token]:
    """Split ``line`` into words and ``>``/``>>`` operators, honouring quotes and backslashes."""
    tokens: list[Token] = []
    index, length = 0, len(line)
    while index < length:
        char = line[index]
        if char.isspace():
            index += 1
            continue
        if char == ">":
            double = line.startswith(">>", index)
            tokens.append(Token(">>" if double else ">", operator=True))
            index += 2 if double else 1
            continue
        if char in UNSUPPORTED_OPERATORS:
            raise ShellSyntaxError(
                f"'{char}' is not supported here (no pipes, background jobs or command lists; use one command per line)"
            )
        buffer: list[str] = []
        single_quoted = False
        while index < length and not line[index].isspace() and line[index] not in UNSUPPORTED_OPERATORS + ">":
            current = line[index]
            if current in "\"'":
                quote = current
                single_quoted = single_quoted or quote == "'"
                index += 1
                while True:
                    if index >= length:
                        raise ShellSyntaxError(f"unmatched {quote!r} quote")
                    if line[index] == quote:
                        index += 1
                        break
                    if quote == '"' and line[index] == "\\" and index + 1 < length:
                        buffer.append(line[index + 1])  # "only escapes inside double quotes
                        index += 2
                        continue
                    buffer.append(line[index])
                    index += 1
                continue
            if current == "\\" and index + 1 < length:
                buffer.append(line[index + 1])
                index += 2
                continue
            buffer.append(current)
            index += 1
        tokens.append(Token("".join(buffer), quoted=single_quoted))
    return tokens


def parse(tokens: Sequence[Token]) -> ParsedLine | None:
    """Turn tokens into a command, its arguments and at most one redirection."""
    if not tokens:
        return None
    redirect_index = next((index for index, token in enumerate(tokens) if token.operator), None)
    if redirect_index is None:
        return ParsedLine(tokens[0].value, [token.value for token in tokens[1:]])
    if redirect_index == 0:
        raise ShellSyntaxError(f"expected a command before '{tokens[0].value}'")
    operator = tokens[redirect_index].value
    rest = list(tokens[redirect_index + 1 :])
    if any(token.operator for token in rest):
        raise ShellSyntaxError("only one redirection per line is supported")
    if len(rest) != 1 or not rest[0].value:
        raise ShellSyntaxError(f"expected exactly one file name after '{operator}'")
    return ParsedLine(
        tokens[0].value, [token.value for token in tokens[1:redirect_index]], operator, rest[0].value
    )


# --------------------------------------------------------------------------- #
# the shell
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CommandInfo:
    """A built-in: how to call it, what it does, and the function that does it."""

    name: str
    usage: str
    summary: str
    run: Callable[[TinyOS, list[str]], str]


COMMANDS: dict[str, CommandInfo] = {}
ALIASES: dict[str, str] = {"ll": "ls -l", "dir": "ls", "cls": "clear", "logout": "exit"}


def command(
    usage: str, summary: str
) -> Callable[[Callable[[TinyOS, list[str]], str]], Callable[[TinyOS, list[str]], str]]:
    """Register a built-in and document it in the same place (help/man read this).

    Dispatch goes through this registry rather than ``getattr(self, "cmd_" + name)``,
    so a method that is not meant to be a command can never become one.
    """

    def decorator(function: Callable[[TinyOS, list[str]], str]) -> Callable[[TinyOS, list[str]], str]:
        name = function.__name__[4:]
        COMMANDS[name] = CommandInfo(name, usage, summary, function)
        return function

    return decorator


class FileSystem:
    """The virtual disk: a node tree, the current directory and the save file.

    Deliberately shell-free -- every path it is handed has already been expanded,
    so the tree can be used (and tested) on its own.  Errors are :class:`OSError`
    subclasses with the path first, the way coreutils phrase them.
    """

    def __init__(
        self,
        root: dict[str, Any] | None = None,
        *,
        save_file: Path | None = None,
        cwd: str = HOME,
    ):
        self.root = root or fresh_fs()
        self.save_file = save_file
        self.cwd = cwd
        self.old_cwd: str | None = None
        self.dirty = False
        self.save_problems = 0

    def split(self, path: str) -> list[str]:
        """Normalise an already-expanded path into root-relative parts ('.' and '..' resolved)."""
        text = path or "."
        if not text.startswith("/"):
            text = self.cwd if text == "." else f"{self.cwd}/{text}"
        parts: list[str] = []
        for part in PurePosixPath(text).parts:
            if part in ("/", "", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return parts

    @staticmethod
    def path_of(parts: Sequence[str]) -> str:
        return "/" + "/".join(parts)

    def absolute(self, path: str) -> str:
        return self.path_of(self.split(path))

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path)
        except OSError:
            return False
        return True

    def resolve_detailed(self, path: str) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        """Return ``(node, entry name, parent node)``; the parent is None for ``/``."""
        parts = self.split(path)
        node = self.root
        parent: dict[str, Any] | None = None
        name = "/"
        for index, part in enumerate(parts):
            if not is_dir(node):
                raise NotADirectoryError(f"{self.path_of(parts[:index]) or '/'}: Not a directory")
            children = children_of(node)
            if part not in children:
                raise FileNotFoundError(f"{self.path_of(parts[: index + 1])}: No such file or directory")
            parent, name, node = node, part, children[part]
        return node, name, parent

    def resolve(self, path: str) -> dict[str, Any]:
        """Return the node ``path`` points at, or raise a useful OSError."""
        return self.resolve_detailed(path)[0]

    def resolve_parent(self, path: str, *, create_parents: bool = False) -> tuple[dict[str, Any], str]:
        """Return ``(parent node, leaf name)`` for a path whose leaf may not exist yet."""
        parts = self.split(path)
        if not parts:
            raise PermissionError(f"{path or '/'}: cannot modify the root directory")
        node = self.root
        for index, part in enumerate(parts[:-1]):
            if not is_dir(node):
                raise NotADirectoryError(f"{self.path_of(parts[: index + 1])}: Not a directory")
            children = node.setdefault("children", {})
            if part not in children:
                if not create_parents:
                    raise FileNotFoundError(f"{self.path_of(parts[: index + 1])}: No such file or directory")
                children[part] = make_dir()
                self.dirty = True
            node = children[part]
        if not is_dir(node):
            raise NotADirectoryError(f"{self.path_of(parts[:-1]) or '/'}: Not a directory")
        leaf = parts[-1]
        if len(leaf.encode("utf-8")) > MAX_NAME_LENGTH:
            raise OSError(f"{leaf}: File name too long")
        return node, leaf

    def read_text(self, path: str) -> str:
        node = self.resolve(path)
        if not is_file(node):
            raise IsADirectoryError(f"{path}: Is a directory")
        content = node.get("content", "")
        return content if isinstance(content, str) else str(content)

    def write_file(self, path: str, content: str) -> None:
        parent, name = self.resolve_parent(path)
        existing = parent["children"].get(name)
        if existing is not None and not is_file(existing):
            raise IsADirectoryError(f"{path}: Is a directory")
        parent["children"][name] = make_file(content)
        self.dirty = True

    def touch(self, path: str) -> None:
        parent, name = self.resolve_parent(path)
        existing = parent["children"].get(name)
        if existing is None:
            parent["children"][name] = make_file("")
        elif not is_file(existing):
            raise IsADirectoryError(f"{path}: Is a directory")
        else:
            existing["mtime"] = now()
        self.dirty = True

    def remove(self, path: str) -> None:
        parent, leaf = self.resolve_parent(path)
        del parent["children"][leaf]
        self.dirty = True

    def is_inside(self, candidate: str, ancestor: str) -> bool:
        """True when ``candidate`` lies below ``ancestor`` in the tree."""
        candidate_parts, ancestor_parts = self.split(candidate), self.split(ancestor)
        return (
            len(ancestor_parts) < len(candidate_parts)
            and candidate_parts[: len(ancestor_parts)] == ancestor_parts
        )

    def is_directory(self, path: str) -> bool:
        """True when ``path`` exists and is a directory."""
        return is_dir(self.resolve(path))

    def chdir(self, path: str) -> None:
        """Move into ``path``, remembering where we came from so ``cd -`` works."""
        node = self.resolve(path)
        if not is_dir(node):
            raise NotADirectoryError(f"{path}: Not a directory")
        self.old_cwd, self.cwd = self.cwd, self.absolute(path)

    def save(self, *, force: bool = False) -> str | None:
        """Atomically persist the virtual disk; return a warning line when that failed.

        Skipping the write when nothing is dirty keeps a session cheap; ``os.replace``
        means a crash mid-save cannot leave a half-written disk behind.
        """
        if self.save_file is None or (not self.dirty and not force):
            return None
        temporary = self.save_file.with_name(self.save_file.name + ".tmp")
        try:
            self.save_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.root, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, self.save_file)
        except OSError as exc:  # a read-only or full disk must not kill the session
            temporary.unlink(missing_ok=True)
            self.save_problems += 1
            return f"cannot save {self.save_file} ({exc}); keeping changes in memory only"
        self.save_problems = 0
        self.dirty = False
        return None

    @classmethod
    def open(cls, save_file: Path | None) -> tuple[FileSystem, str | None]:
        """Boot from ``save_file`` when it is readable, otherwise from a fresh disk."""
        root, problem = load_fs(save_file) if save_file else (fresh_fs(), None)
        return cls(root, save_file=save_file), problem


class TinyOS:
    """The shell: expansion, dispatch and session state on top of a :class:`FileSystem`."""

    def __init__(
        self,
        root: dict[str, Any] | None = None,
        *,
        save_file: Path | None = None,
        read_line: Callable[[str], str | None] | None = None,
        fs: FileSystem | None = None,
    ):
        self.fs = fs if fs is not None else FileSystem(root, save_file=save_file)
        self.history: list[str] = []
        self.read_line = read_line
        self.should_exit = False
        self.exit_code = 0
        self.unicode_output = True
        self.boot_time = now()
        self.hostname = "osmaker"  # replaced from /etc/hostname below, once expansion works
        try:
            raw_hostname = self.read_text("/etc/hostname").strip()
        except OSError:
            raw_hostname = ""
        if HOSTNAME_RE.match(raw_hostname):
            self.hostname = raw_hostname

    # -- the disk, with shell expansion in front ---------------------------- #
    @property
    def root(self) -> dict[str, Any]:
        return self.fs.root

    @property
    def cwd(self) -> str:
        return self.fs.cwd

    @cwd.setter
    def cwd(self, value: str) -> None:
        self.fs.cwd = value

    @property
    def old_cwd(self) -> str | None:
        return self.fs.old_cwd

    @property
    def save_file(self) -> Path | None:
        return self.fs.save_file

    @property
    def save_problems(self) -> int:
        return self.fs.save_problems

    @property
    def dirty(self) -> bool:
        return self.fs.dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self.fs.dirty = value

    def split(self, path: str) -> list[str]:
        """Expand (shell) then normalise (filesystem) a user path."""
        return self.fs.split(self.expand(path))

    def absolute(self, path: str) -> str:
        return self.fs.path_of(self.split(path))

    @staticmethod
    def path_of(parts: Sequence[str]) -> str:
        """Join root-relative parts back into a path (used when a copy needs a new leaf)."""
        return FileSystem.path_of(parts)

    def exists(self, path: str) -> bool:
        return self.fs.exists(self.expand(path))

    def resolve(self, path: str) -> dict[str, Any]:
        return self.fs.resolve(self.expand(path))

    def resolve_detailed(self, path: str) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        return self.fs.resolve_detailed(self.expand(path))

    def resolve_parent(self, path: str, *, create_parents: bool = False) -> tuple[dict[str, Any], str]:
        return self.fs.resolve_parent(self.expand(path), create_parents=create_parents)

    def is_inside(self, candidate: str, ancestor: str) -> bool:
        return self.fs.is_inside(self.expand(candidate), self.expand(ancestor))

    def read_text(self, path: str) -> str:
        return self.fs.read_text(self.expand(path))

    def write_file(self, path: str, content: str) -> None:
        self.fs.write_file(self.expand(path), content)

    def touch(self, path: str) -> None:
        self.fs.touch(self.expand(path))

    def remove(self, path: str) -> None:
        self.fs.remove(self.expand(path))

    def is_directory(self, path: str) -> bool:
        """True when ``path`` exists and is a directory."""
        return is_dir(self.resolve(path))

    def chdir(self, path: str) -> None:
        self.fs.chdir(self.expand(path))

    def save(self, *, force: bool = False) -> None:
        problem = self.fs.save(force=force)
        if problem and self.fs.save_problems == 1:  # warn once, then stay quiet
            print(f"ossh: {problem}", file=sys.stderr)

    # -- read-eval-print ------------------------------------------------------ #
    def read_input(self, prompt: str) -> str | None:
        """One line from the injected reader (a test can supply its own)."""
        reader = self.read_line or input
        return reader(prompt)

    def feed(self, lines: Iterable[str]) -> int:
        """Run ``lines`` in order, saving after each, stopping at ``exit``; the status wins."""
        for line in lines:
            output = self.run(line)
            if output:
                print(output)
            self.save()
            if self.should_exit:
                break
        return self.exit_code

    def interact(self) -> int:
        """Read, evaluate, print, save -- until end of input or ``exit``."""
        while True:
            try:
                line = self.read_input(prompt_for(self))
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("^C")
                self.exit_code = 130
                continue
            if line is None:  # a reader that reports EOF without raising
                print()
                break
            output = self.run(line)
            if output:
                print(output)
            self.save()
            if self.should_exit:
                break
        return self.exit_code

    # -- shell environment --------------------------------------------------- #
    def environment(self) -> dict[str, str]:
        return {
            "HOME": HOME,
            "USER": USER,
            "PWD": self.cwd,
            "OLDPWD": self.old_cwd or "",
            "HOSTNAME": self.hostname,
            "SHELL": "/bin/ossh",
            "OSMAKER": VERSION,
            "PATH": "/bin:/usr/bin:/sbin:/usr/sbin",
        }

    def expand(self, text: str, *, quoted: bool = False) -> str:
        """Substitute ``~``, ``$VAR``, ``${VAR}`` and ``$?``; single-quoted words stay literal."""
        if quoted:
            return text
        text = text.replace("$?", str(self.exit_code))
        if text == "~":
            text = HOME
        elif text.startswith("~/"):
            text = HOME + text[1:]
        environment = self.environment()
        return VARIABLE_RE.sub(lambda match: environment.get(match.group(1) or match.group(2), ""), text)

    # -- execution ---------------------------------------------------------- #
    def run(self, line: str) -> str:
        """Execute one command line and return the text it printed (errors included)."""
        line = line.strip()
        if not line or line.startswith("#"):
            return ""
        self.history.append(line)
        label = "ossh"
        try:
            raw_tokens = lex(line)
            tokens = [
                Token(self.expand(token.value, quoted=token.quoted), operator=token.operator)
                for token in raw_tokens
            ]
            parsed = parse(tokens)
            if parsed is None:
                return ""
            label = parsed.command
            command_name, args = parsed.command, parsed.args
            if command_name in ALIASES:
                alias_tokens = lex(ALIASES[command_name])
                label = command_name = alias_tokens[0].value
                args = [token.value for token in alias_tokens[1:]] + parsed.args
            info = COMMANDS.get(command_name)
            if info is None:
                self.exit_code = 127
                return f"{command_name}: command not found (try 'help')"
            output = info.run(self, args) or ""
            if not self.should_exit:  # `exit 3` sets its own status
                self.exit_code = 0
            if parsed.redirect and parsed.target:
                if parsed.redirect == ">>":
                    try:
                        existing = self.read_text(parsed.target)
                    except FileNotFoundError:
                        existing = ""
                    if existing.strip():
                        output = existing.rstrip("\n") + "\n" + output
                self.write_file(parsed.target, output + "\n" if output else "")
                return ""
            return output
        except ShellSyntaxError as exc:
            self.exit_code = 2
            return f"{label}: syntax error: {exc}"
        except (OSError, ValueError) as exc:
            message = str(exc) or exc.__class__.__name__
            self.exit_code = 1
            return message if message.startswith(f"{label}:") else f"{label}: {message}"

    # -- built-ins ---------------------------------------------------------- #
    @command("help [command]", "show this help, or the help for one command")
    def cmd_help(self, args: list[str]) -> str:
        if args:
            return self.cmd_man(args)
        width = max(len(name) for name in COMMANDS) + 2
        lines = [f"OS Maker {VERSION} -- {len(COMMANDS)} built-in commands"]
        lines += [f"  {name.ljust(width)}{COMMANDS[name].summary}" for name in sorted(COMMANDS)]
        lines.append("")
        lines.append(f"Aliases: {', '.join(f'{a} -> {ALIASES[a]}' for a in sorted(ALIASES))}")
        lines.append("Quotes are honoured, ~ and $VAR expand, and '> file' / '>> file' redirect output.")
        lines.append(f"Virtual disk: {self.save_file or 'in memory only (--no-save)'}")
        return "\n".join(lines)

    @command("man <command>", "show the usage line of one command")
    def cmd_man(self, args: list[str]) -> str:
        if not args:
            raise ValueError("man: what manual page do you want?")
        name = ALIASES.get(args[0], args[0]).split()[0]
        info = COMMANDS.get(name)
        if info is None:
            raise ValueError(f"no manual entry for {args[0]}")
        return f"{info.name.upper()}({info.usage})\n\n  {info.summary}\n\n  Usage: {info.usage}\n"

    @command("pwd", "print the current directory")
    def cmd_pwd(self, args: list[str]) -> str:
        self.reject_extra(args, "pwd")
        return self.cwd

    @command("ls [-l] [-a] [path...]", "list files and directories")
    def cmd_ls(self, args: list[str]) -> str:
        flags, paths = self.take_flags(args, "la")
        long_format, show_all = "l" in flags, "a" in flags
        for path in paths:  # validate early so a typo does not print half the output
            self.resolve(path)
        blocks: list[str] = []
        for path in paths or ["."]:
            node = self.resolve(path)
            if not is_dir(node):
                blocks.append(f"{path}: {node_size(node)}B")
                continue
            children = children_of(node)
            names = sorted(children)
            if not show_all:
                names = [name for name in names if not name.startswith(".")]
            if len(paths) > 1:
                blocks.append(f"{path}:")
            if not names:
                blocks.append("total 0" if long_format else "(empty)")
                continue
            if long_format:
                rows = [f"total {sum(node_blocks(children[name]) for name in names)}"]
                for name in names:
                    child = children[name]
                    entry_is_dir = is_dir(child)
                    rows.append(
                        f"{'drwxr-xr-x' if entry_is_dir else '-rw-r--r--'} {link_count(child):>3} {USER:<5} "
                        f"{content_size(child):>8} "
                        f"{time.strftime('%b %d %H:%M', time.localtime(mtime_of(child, self.boot_time)))} "
                        f"{name}{'/' if entry_is_dir else ''}"
                    )
                blocks.append("\n".join(rows))
            else:
                blocks.append(
                    "  ".join(f"{name}{'/' if children[name].get('type') == 'dir' else ''}" for name in names)
                )
        return "\n\n".join(blocks)

    @command("cd [path|-]", "change directory (no argument goes home, '-' goes back)")
    def cmd_cd(self, args: list[str]) -> str:
        self.reject_extra(args, "cd", limit=1)
        target = args[0] if args else HOME
        if target == "-":
            if not self.old_cwd:
                raise OSError("cd: OLDPWD not set")
            target = self.old_cwd
        self.chdir(target)
        return ""

    @command("mkdir [-p] <dir>...", "create directories")
    def cmd_mkdir(self, args: list[str]) -> str:
        flags, names = self.take_flags(args, "p")
        if not names:
            raise ValueError("missing operand after 'mkdir'")
        parents = "p" in flags
        for name in names:
            parent, leaf = self.resolve_parent(name, create_parents=parents)
            existing = parent["children"].get(leaf)
            if existing is not None:
                if existing.get("type") == "dir" and parents:
                    continue
                raise FileExistsError(f"{self.expand(name)}: cannot create directory (already exists)")
            parent["children"][leaf] = make_dir()
            self.dirty = True
        return ""

    @command("rmdir <dir>...", "remove empty directories")
    def cmd_rmdir(self, args: list[str]) -> str:
        if not args:
            raise ValueError("missing operand after 'rmdir'")
        for name in args:
            node, _, parent = self.resolve_detailed(name)
            if not is_dir(node):
                raise NotADirectoryError(f"{self.expand(name)}: Not a directory")
            if children_of(node):
                raise OSError(f"{self.expand(name)}: Directory not empty")
            if parent is None:
                raise PermissionError("rmdir: cannot remove '/'")
            self.remove(name)
        return ""

    @command("touch <file>...", "create empty files or update their timestamp")
    def cmd_touch(self, args: list[str]) -> str:
        if not args:
            raise ValueError("missing operand after 'touch'")
        for name in args:
            self.touch(name)
        return ""

    @command("write <file> <text>", "replace the contents of a file")
    def cmd_write(self, args: list[str]) -> str:
        if len(args) < 2:
            raise ValueError("write needs a file and the text to store")
        self.write_file(args[0], " ".join(args[1:]))
        return ""

    @command("append <file> <text>", "add a line to the end of a file")
    def cmd_append(self, args: list[str]) -> str:
        if len(args) < 2:
            raise ValueError("append needs a file and the text to add")
        try:
            existing = self.read_text(args[0])
        except FileNotFoundError:
            existing = ""
        text = " ".join(args[1:])
        joined = f"{existing.rstrip(chr(10))}\n{text}" if existing.strip() else text
        self.write_file(args[0], joined)
        return ""

    @command("cat <file>...", "print file contents")
    def cmd_cat(self, args: list[str]) -> str:
        if not args:
            raise ValueError("cat: missing file operand")
        chunks = [self.read_text(name) for name in args]
        return "\n".join(chunk.rstrip("\n") for chunk in chunks if chunk.strip())

    @command("echo [-n] [text]", "print text (works with '>' and '>>' too)")
    def cmd_echo(self, args: list[str]) -> str:
        if args and args[0] == "-n":
            return " ".join(args[1:])
        return " ".join(args)

    @command("rm [-r] [-f] <path>...", "remove files, or directories with -r")
    def cmd_rm(self, args: list[str]) -> str:
        flags, names = self.take_flags(args, "rfR")
        if not names:
            raise ValueError("rm: missing operand")
        force, recursive = "f" in flags, "r" in flags or "R" in flags
        for name in names:
            try:
                node, _, parent = self.resolve_detailed(name)
            except (FileNotFoundError, NotADirectoryError):
                if force:
                    continue
                raise
            if parent is None:
                raise PermissionError("rm: cannot remove '/': that is the whole virtual disk")
            if is_dir(node) and children_of(node) and not recursive:
                raise OSError(f"{self.expand(name)}: Directory not empty (use -r)")
            self.remove(name)
        return ""

    @command("cp [-r] <source>... <destination>", "copy files (directories need -r)")
    def cmd_cp(self, args: list[str]) -> str:
        flags, names = self.take_flags(args, "rR")
        if len(names) < 2:
            raise ValueError("cp needs at least a source and a destination")
        recursive = "r" in flags or "R" in flags
        destination = names[-1]
        target_node = self.resolve(destination) if self.exists(destination) else None
        into_dir = target_node is not None and is_dir(target_node)
        if not into_dir and len(names) > 2:
            raise NotADirectoryError(f"{destination}: not a directory")
        for source in names[:-1]:
            node = self.resolve(source)
            if is_dir(node) and not recursive:
                raise OSError("-r not specified; omitting directory " + repr(source))
            source_absolute = self.absolute(source)
            target_path = (
                self.path_of([*self.split(destination), PurePosixPath(source_absolute).name])
                if into_dir
                else self.absolute(destination)
            )
            if target_path == source_absolute:
                raise OSError(f"'{source}' and '{target_path}' are the same file")
            if is_dir(node) and self.is_inside(target_path, source_absolute):
                raise OSError(f"cannot copy a directory, '{source}', into itself, '{target_path}'")
            parent, leaf = self.resolve_parent(target_path)
            existing = parent["children"].get(leaf)
            if existing is not None and existing.get("type") != node.get("type"):
                kind = "directory" if is_dir(existing) else "file"
                raise OSError(f"cannot create {target_path}: replacing a {kind} with a different type")
            parent["children"][leaf] = copy.deepcopy(node)
            parent["children"][leaf]["mtime"] = now()
            self.dirty = True
        return ""

    @command("mv <source>... <destination>", "rename or move files")
    def cmd_mv(self, args: list[str]) -> str:
        if len(args) < 2:
            raise ValueError("mv needs at least a source and a destination")
        destination = args[-1]
        target_node = self.resolve(destination) if self.exists(destination) else None
        into_dir = target_node is not None and is_dir(target_node)
        if not into_dir and len(args) > 2:
            raise NotADirectoryError(f"{destination}: not a directory")
        for source in args[:-1]:
            node, leaf, parent = self.resolve_detailed(source)
            if parent is None:
                raise PermissionError("mv: cannot move '/'")
            source_absolute = self.absolute(source)
            target_path = (
                self.path_of([*self.split(destination), leaf]) if into_dir else self.absolute(destination)
            )
            if target_path == source_absolute:
                raise OSError(f"'{source}' and '{destination}' are the same file")
            if is_dir(node) and self.is_inside(target_path, source_absolute):
                raise OSError(f"cannot move '{source}' to a subdirectory of itself, '{target_path}'")
            target_parent, target_leaf = self.resolve_parent(target_path)
            if target_parent is node:  # moving a directory onto itself
                raise OSError("mv: cannot move a directory onto itself")
            existing = target_parent["children"].get(target_leaf)
            if existing is not None and existing.get("type") != node.get("type"):
                kind = "directory" if is_dir(existing) else "file"
                raise OSError(f"cannot move '{source}' over {kind} '{target_path}'")
            if existing is not None and is_dir(existing) and children_of(existing):
                raise OSError(f"cannot move '{source}' into non-empty directory '{target_path}'")
            target_parent["children"][target_leaf] = copy.deepcopy(node)
            target_parent["children"][target_leaf]["mtime"] = now()
            del parent["children"][leaf]
            self.cwd = self.absolute(self.cwd)
            self.dirty = True
        return ""

    @command("tree [path]", "show the directory tree below a path")
    def cmd_tree(self, args: list[str]) -> str:
        self.reject_extra(args, "tree", limit=1)
        path = args[0] if args else "."
        node = self.resolve(path)
        if not is_dir(node):
            return f"{self.absolute(path)}: {node_size(node)}B"
        branch = ("├── ", "└── ", "│   ", "    ") if self.unicode_output else ("|-- ", "`-- ", "|   ", "    ")
        lines = [self.absolute(path)]
        counters = {"dir": 0, "file": 0}

        def walk(current: dict[str, Any], prefix: str) -> None:
            children = sorted(children_of(current))
            for index, name in enumerate(children):
                child = current["children"][name]
                connector, spacer = (
                    (branch[1], branch[3]) if index == len(children) - 1 else (branch[0], branch[2])
                )
                lines.append(f"{prefix}{connector}{name}{'/' if child.get('type') == 'dir' else ''}")
                counters["dir" if is_dir(child) else "file"] += 1
                if is_dir(child):
                    walk(child, prefix + spacer)

        walk(node, "")
        directories, files = counters["dir"], counters["file"]
        lines.append(
            f"\n{directories} director{'ies' if directories != 1 else 'y'}, {files} file{'s' if files != 1 else ''}"
        )
        return "\n".join(lines)

    @command("stat <path>", "show metadata for a file or directory")
    def cmd_stat(self, args: list[str]) -> str:
        if not args:
            raise ValueError("stat: missing operand")
        self.reject_extra(args, "stat", limit=1)
        node = self.resolve(args[0])
        return "\n".join(
            [
                f"  File: {self.absolute(args[0])}",
                f"  Size: {content_size(node)}\tBlocks: {node_blocks(node)}\tIO Block: {BLOCK_SIZE}",
                f"  Type: {'directory' if node.get('type') == 'dir' else 'regular file'}",
                f"  Links: {link_count(node)}\tUid: 1000 ({USER})\tGid: 1000 (staff)",
                f"  Modify: {format_time(mtime_of(node, self.boot_time))}",
            ]
        )

    @command("df", "show how much of the virtual disk is used")
    def cmd_df(self, args: list[str]) -> str:
        self.reject_extra(args, "df")
        used = node_blocks(self.root)
        free = max(0, DISK_BLOCKS - used)
        capacity = int(used * 100 / DISK_BLOCKS) if DISK_BLOCKS else 0
        head = f"{'Filesystem':<14}{'1K-blocks':>11}{'Used':>9}{'Available':>11}{'Use%':>6}  Mounted on"
        row = f"{'/dev/osda1':<14}{DISK_BLOCKS:>11}{used:>9}{free:>11}{f'{capacity}%':>6}  /"
        return f"{head}\n{row}"

    @command("du [path]", "show how much space each entry uses, in 1K blocks")
    def cmd_du(self, args: list[str]) -> str:
        self.reject_extra(args, "du", limit=1)
        path = args[0] if args else "."
        node = self.resolve(path)
        absolute = self.absolute(path)
        if not is_dir(node):
            return f"{node_blocks(node):>7}\t{absolute}"
        rows = [
            f"{node_blocks(child):>7}\t{absolute.rstrip('/')}/{name}"
            for name, child in sorted(children_of(node).items())
        ]
        rows.append(f"{node_blocks(node):>7}\t{absolute}")
        return "\n".join(rows)

    @command("date", "show the clock")
    def cmd_date(self, args: list[str]) -> str:
        self.reject_extra(args, "date")
        return time.strftime("%a %d %b %Y %H:%M:%S %z", time.localtime()).strip()

    @command("whoami", "print the current user")
    def cmd_whoami(self, args: list[str]) -> str:
        self.reject_extra(args, "whoami")
        return USER

    @command("env", "print the environment variables")
    def cmd_env(self, args: list[str]) -> str:
        self.reject_extra(args, "env")
        return "\n".join(f"{key}={value}" for key, value in sorted(self.environment().items()))

    @command("hostname [name]", "show or set the system hostname")
    def cmd_hostname(self, args: list[str]) -> str:
        self.reject_extra(args, "hostname", limit=1)
        if not args:
            return self.hostname
        candidate = args[0].strip()
        if not HOSTNAME_RE.match(candidate):
            raise ValueError(f"{candidate!r} is not a valid hostname (letters, digits and '-')")
        self.hostname = candidate
        self.write_file("/etc/hostname", candidate + "\n")
        return ""

    @command("uname [-a]", "print system information")
    def cmd_uname(self, args: list[str]) -> str:
        self.reject_extra(args, "uname", limit=1)
        if args and args[0] not in ("-a", "--all"):
            raise ValueError(f"invalid option {args[0]!r}")
        fields = [
            "OSMaker",
            self.hostname,
            VERSION,
            f"#1-OsMaker SMP {time.strftime('%b %d %Y')}",
            platform.machine() or "unknown",
            "GNU/Linux",
        ]
        return " ".join(fields) if args else fields[0]

    @command("uptime", "how long this session has been running")
    def cmd_uptime(self, args: list[str]) -> str:
        self.reject_extra(args, "uptime")
        seconds = max(0, now() - self.boot_time)
        clock = time.strftime("%H:%M:%S", time.localtime())
        return f"{clock} up 0:{seconds // 60:02d}:{seconds % 60:02d}, 1 user, load average: 0.00, 0.01, 0.05"

    @command("history", "list the commands typed in this session")
    def cmd_history(self, args: list[str]) -> str:
        self.reject_extra(args, "history")
        return "\n".join(f"{number:>4}  {entry}" for number, entry in enumerate(self.history, start=1))

    @command("edit <file>", "edit a file by typing lines ('.' on its own finishes)")
    def cmd_edit(self, args: list[str]) -> str:
        self.reject_extra(args, "edit", limit=1)
        if not args:
            raise ValueError("edit: missing file operand")
        if self.read_line is None:
            raise OSError("no terminal to read from (use 'write <file> <text>' instead)")
        try:
            existing = self.read_text(args[0])
        except FileNotFoundError:
            existing = ""
        lines = existing.rstrip("\n").split("\n") if existing.strip() else []
        while True:
            try:
                entry = self.read_line(f"  {len(lines) + 1}> ")
            except EOFError:
                entry = None
            if entry is None or entry.strip() == ".":
                break
            lines.append(entry)
        self.write_file(args[0], "\n".join(lines) + ("\n" if lines else ""))
        return f"wrote {len(lines)} line{'s' if len(lines) != 1 else ''} to {self.absolute(args[0])}"

    @command("clear", "clear the screen")
    def cmd_clear(self, args: list[str]) -> str:
        self.reject_extra(args, "clear")
        return "\x1b[2J\x1b[H" if sys.stdout.isatty() else "[screen cleared]"

    @command("exit [code]", "shut down (the virtual disk is saved first)")
    def cmd_exit(self, args: list[str]) -> str:
        self.reject_extra(args, "exit", limit=1)
        self.exit_code = 0
        if args:
            try:
                self.exit_code = int(args[0])
            except ValueError:
                raise ValueError("numeric argument required") from None
        self.should_exit = True
        return ""

    # -- argument helpers --------------------------------------------------- #
    @staticmethod
    def reject_extra(args: Sequence[str], name: str, *, limit: int = 0) -> None:
        if len(args) > limit:
            raise ValueError(f"{name}: extra operand {args[limit]!r} (see 'man {name}')")

    @staticmethod
    def take_flags(args: Sequence[str], letters: str) -> tuple[str, list[str]]:
        """Split ``args`` into single-letter flags and operands, rejecting unknown flags."""
        flags: list[str] = []
        operands: list[str] = []
        after_double_dash = False
        for arg in args:
            if after_double_dash:
                operands.append(arg)
            elif arg == "--":
                after_double_dash = True
            elif len(arg) > 1 and arg.startswith("-") and not arg.startswith("--"):
                for flag in arg[1:]:
                    if flag not in letters:
                        raise ValueError(f"invalid option -- '{flag}'")
                    flags.append(flag)
            elif arg.startswith("-"):
                raise ValueError(f"unrecognized option {arg!r}")
            else:
                operands.append(arg)
        return "".join(flags), operands


# --------------------------------------------------------------------------- #
# persistence and start-up
# --------------------------------------------------------------------------- #
def load_fs(save_file: Path) -> tuple[dict[str, Any], str | None]:
    """Read a saved filesystem; on any problem return a fresh one and why."""
    try:
        raw = save_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh_fs(), None
    except OSError as exc:
        return fresh_fs(), f"could not read {save_file} ({exc}); starting from a fresh filesystem"
    try:
        data = json.loads(raw)
        validate_fs(data)
    except (json.JSONDecodeError, ValueError) as exc:
        return (
            fresh_fs(),
            f"{save_file.name} is not a valid virtual disk ({exc}); starting from a fresh filesystem",
        )
    return data, None


def supports_unicode(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    encoding = (getattr(stream, "encoding", "") or "").lower()
    try:
        "├─".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def banner(system: TinyOS) -> str:
    """A short boot screen: logo, version, and the message of the day."""
    colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    cyan, reset = ("\033[1;36m", "\033[0m") if colour else ("", "")
    if system.unicode_output:
        art = [
            " ██████╗ ███████╗",
            "██╔═══██╗██╔════╝",
            "██║   ██║███████╗",
            "██║   ██║╚════██║",
            "╚██████╔╝███████║",
            " ╚═════╝ ╚══════╝",
        ]
    else:  # plain ASCII for terminals that cannot draw the block characters
        art = ["+" + "-" * 20 + "+", "|   O S   M A K E R   |", "+" + "-" * 20 + "+"]
    notes = [
        f"M A K E R {VERSION}",
        "a mini operating system that runs in your terminal",
        f"{USER}@{system.hostname}, cwd {display_cwd(system)}",
    ]
    pad = max(len(line) for line in art) + 3

    def compose(left: str, right: str) -> str:
        """Place the note beside the logo, padding the visible text (not the escape codes)."""
        combined = (left.ljust(pad) + right).rstrip()
        head = combined[: min(len(combined), len(left))]
        return f"{cyan}{head}{reset}{combined[len(head) :]}"

    merged = "\n".join(
        compose(left, right) for left, right in itertools.zip_longest(art, notes, fillvalue="")
    )
    lines = [merged]
    try:
        motd = system.read_text("/etc/motd").strip()
    except OSError:
        motd = ""
    if motd:
        lines.append("")
        lines.extend(f"  {line}" for line in motd.splitlines())
    return "\n".join(lines)


def display_cwd(system: TinyOS) -> str:
    return "~" if system.cwd == HOME else system.cwd


def prompt_for(system: TinyOS) -> str:
    cwd = display_cwd(system)
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\033[1;32m{USER}@{system.hostname}\033[0m:\033[1;34m{cwd}\033[0m$ "
    return f"{USER}@{system.hostname}:{cwd}$ "


def make_completer(readline: Any, system: TinyOS) -> Callable[[str, int], str | None]:
    """Build a readline completer: command names first, then paths anywhere else."""

    state_cache: dict[str, list[str]] = {"matches": []}

    def path_matches(text: str) -> list[str]:
        directory, _, prefix = text.rpartition("/")
        try:
            node = system.resolve(directory or ".")
        except OSError:
            return []
        if not is_dir(node):
            return []
        matches = []
        for name, child in sorted(children_of(node).items()):
            if not name.startswith(prefix):
                continue
            suffix = "/" if is_dir(child) else " "
            matches.append(f"{directory}/{name}{suffix}" if directory else f"{name}{suffix}")
        return matches

    def complete(text: str, state: int) -> str | None:
        if state == 0:
            buffer = readline.get_line_buffer()
            completing_first_word = not buffer[: readline.get_begidx()].strip()
            if completing_first_word:
                pool = sorted(set(COMMANDS) | set(ALIASES))
                state_cache["matches"] = [name + " " for name in pool if name.startswith(text)]
            else:
                state_cache["matches"] = path_matches(text)
        matches = state_cache["matches"]
        return matches[state] if state < len(matches) else None

    return complete


def setup_readline(system: TinyOS, *, persist_history: bool = True) -> Callable[[], None] | None:
    """Wire up line editing, history and completion; returns a function that saves history."""
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows without pyreadline
        return None
    state_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "os-maker"
    history_file = state_dir / "history"
    try:
        if history_file.is_file():
            readline.read_history_file(history_file)
    except OSError:
        pass
    with contextlib.suppress(Exception):  # pragma: no cover - readline quirks
        for entry in system.history:
            readline.add_history(entry)
    readline.set_completer(make_completer(readline, system))
    readline.set_completer_delims(" \t\n\"'")
    try:
        readline.parse_and_bind("set show-all-if-ambiguous on")
        readline.parse_and_bind("tab: complete")
    except AttributeError:  # pragma: no cover - non-GNU readline
        pass

    def save_history() -> None:
        if not persist_history:
            return
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(history_file)
        except OSError:
            pass

    return save_history


def boot(*, save_file: Path | None = None, ascii_only: bool = False) -> tuple[TinyOS, str | None]:
    """Start a shell on ``save_file``; the second value explains any load problem.

    Kept separate from :func:`main` so a session (including a broken disk) can be
    driven from a test without spawning a process.
    """
    disk, problem = FileSystem.open(save_file)
    system = TinyOS(fs=disk, read_line=input)
    if ascii_only or not supports_unicode():
        system.unicode_output = False
    return system, problem


def shutdown(system: TinyOS) -> str:
    """Write the disk one last time and phrase the goodbye accordingly."""
    if system.save_file is None:
        return "OS Maker shut down (nothing was saved)."
    system.save(force=not system.save_file.exists())  # a --reset disk still has to land
    if system.save_problems:
        return "OS Maker shut down, but the disk could not be written -- see the warning above."
    return "OS Maker shut down. Your virtual disk has been saved."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="os_maker.py",
        description="A tiny, safe, persistent operating-system simulator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run without arguments for an interactive shell. -e/--execute runs commands and quits.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "-e",
        "--execute",
        action="append",
        default=[],
        metavar="COMMAND",
        help="run one command, print its output, then continue (repeatable; stops at 'exit')",
    )
    parser.add_argument(
        "--save-file",
        type=Path,
        metavar="PATH",
        help="virtual disk to load and save (default: os_maker_data.json next to this script)",
    )
    parser.add_argument("--no-save", action="store_true", help="keep the filesystem in memory only")
    parser.add_argument("--reset", action="store_true", help="discard the saved filesystem before booting")
    parser.add_argument("--no-banner", action="store_true", help="skip the boot banner")
    parser.add_argument("--ascii", action="store_true", help="plain ASCII instead of box-drawing characters")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    save_file = None if args.no_save else Path(args.save_file or DEFAULT_SAVE_FILE).expanduser().resolve()
    if save_file is not None and args.reset:
        try:
            save_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"ossh: could not reset {save_file} ({exc})", file=sys.stderr)
            return 1
    system, problem = boot(save_file=save_file, ascii_only=args.ascii)
    if problem:
        print(f"ossh: {problem}", file=sys.stderr)
    if not args.no_banner:
        print(banner(system))
    # Line editing, completion and shell history are for interactive use only; the
    # persistent history file is skipped for --no-save (a throwaway session).
    save_history = None
    if not args.execute and sys.stdin is not None and sys.stdin.isatty():
        save_history = setup_readline(system, persist_history=not args.no_save)
    try:
        status = system.feed(args.execute) if args.execute else system.interact()
    finally:
        if save_history:
            save_history()
        print(shutdown(system))
    return status


if __name__ == "__main__":
    sys.exit(main())
