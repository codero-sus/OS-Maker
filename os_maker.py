#!/usr/bin/env python3
"""OS Maker: a tiny, safe, persistent operating-system simulator."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SAVE_FILE = Path(__file__).with_name("os_maker_data.json")


def fresh_fs() -> dict[str, Any]:
    return {"type": "dir", "children": {
        "home": {"type": "dir", "children": {
            "guest": {"type": "dir", "children": {
                "welcome.txt": {"type": "file", "content": "Welcome to your new system!\nType 'help' to see available commands.\n"}
            }}
        }},
        "etc": {"type": "dir", "children": {}},
        "tmp": {"type": "dir", "children": {}},
    }}


class TinyOS:
    def __init__(self, root: dict[str, Any] | None = None):
        self.root = root or fresh_fs()
        self.cwd = "/home/guest"

    def resolve(self, path: str = ".") -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        full = PurePosixPath(path if path.startswith("/") else f"{self.cwd}/{path}")
        parts = [p for p in full.parts if p not in ("/", "", ".")]
        normalized: list[str] = []
        for part in parts:
            if part == "..":
                if normalized:
                    normalized.pop()
            else:
                normalized.append(part)
        node = self.root
        parent = None
        name = "/"
        for part in normalized:
            if node["type"] != "dir" or part not in node["children"]:
                raise FileNotFoundError("No such file or directory")
            parent, name = node, part
            node = node["children"][part]
        return node, name, parent

    @staticmethod
    def need(args: list[str], count: int, usage: str) -> None:
        if len(args) < count:
            raise ValueError(f"Usage: {usage}")

    def run(self, line: str) -> str:
        try:
            parts = shlex.split(line)
            if not parts:
                return ""
            cmd, args = parts[0], parts[1:]
            if cmd == "help":
                return """OS Maker commands:
  help                 Show this help
  pwd                  Show the current directory
  ls [path]            List files and folders
  cd [path]            Change directory
  mkdir <name>         Create a folder
  touch <file>         Create an empty file
  write <file> <text>  Replace a file's contents
  cat <file>           Print a file
  rm <path>            Remove a file or empty folder
  clear                Clear the screen
  exit                 Shut down OS Maker
"""
            if cmd == "pwd":
                return self.cwd
            if cmd == "ls":
                node, _, _ = self.resolve(args[0] if args else ".")
                if node["type"] != "dir":
                    return args[0] if args else ""
                names = sorted(node["children"])
                return "  ".join(n + ("/" if node["children"][n]["type"] == "dir" else "") for n in names) or "(empty)"
            if cmd == "cd":
                node, _, _ = self.resolve(args[0] if args else "/home/guest")
                if node["type"] != "dir":
                    raise NotADirectoryError("Not a directory")
                path = PurePosixPath(args[0] if args else "/home/guest")
                self.cwd = str(path if path.is_absolute() else PurePosixPath(self.cwd) / path)
                # Normalize cwd through the same traversal rules.
                self.cwd = "/" + "/".join(self._cwd_parts(self.cwd))
                return ""
            if cmd in ("mkdir", "touch", "write"):
                self.need(args, 1, {"mkdir":"mkdir <name>", "touch":"touch <file>", "write":"write <file> <text>"}[cmd])
                if cmd == "write":
                    self.need(args, 2, "write <file> <text>")
                target = args[0]
                parent_path, _, leaf = target.rpartition("/")
                parent, _, _ = self.resolve(parent_path or ("/" if target.startswith("/") else "."))
                if parent["type"] != "dir":
                    raise NotADirectoryError("Parent is not a directory")
                if not leaf:
                    leaf = target
                if leaf in (".", "..", ""):
                    raise ValueError("Invalid name")
                if cmd == "mkdir":
                    if leaf in parent["children"]:
                        raise FileExistsError("Already exists")
                    parent["children"][leaf] = {"type": "dir", "children": {}}
                else:
                    if leaf in parent["children"] and parent["children"][leaf]["type"] != "file":
                        raise IsADirectoryError("Is a directory")
                    content = " ".join(args[1:]) if cmd == "write" else parent["children"].get(leaf, {}).get("content", "")
                    parent["children"][leaf] = {"type": "file", "content": content}
                return ""
            if cmd == "cat":
                self.need(args, 1, "cat <file>")
                node, _, _ = self.resolve(args[0])
                if node["type"] != "file":
                    raise IsADirectoryError("Is a directory")
                return node["content"]
            if cmd == "rm":
                self.need(args, 1, "rm <path>")
                node, name, parent = self.resolve(args[0])
                if parent is None:
                    raise PermissionError("Cannot remove the root directory")
                if node["type"] == "dir" and node["children"]:
                    raise OSError("Directory not empty")
                del parent["children"][name]
                return ""
            if cmd == "clear":
                return "\x1b[2J\x1b[H"
            if cmd == "exit":
                raise SystemExit
            return f"{cmd}: command not found (try 'help')"
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, FileExistsError, PermissionError, OSError, ValueError) as exc:
            return f"os: {exc}"

    def _cwd_parts(self, path: str) -> list[str]:
        parts: list[str] = []
        for part in PurePosixPath(path).parts:
            if part in ("/", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(part)
        return parts


def load_fs() -> dict[str, Any]:
    try:
        data = json.loads(SAVE_FILE.read_text(encoding="utf-8"))
        return data if data.get("type") == "dir" and isinstance(data.get("children"), dict) else fresh_fs()
    except (OSError, json.JSONDecodeError, AttributeError):
        return fresh_fs()


def main() -> None:
    os = TinyOS(load_fs())
    print("\033[1;36m  ____  ____     __  __       _ kona\n / __ \/ ___|   |  \/  | __ _| | _____ _ __\n| |  | \___ \   | |\/| |/ _` | |/ / _ \ '__|\n| |__| |___) |  | |  | | (_| |   <  __/ |\n \____/|____/   |_|  |_|\__,_|_|\_\___|_|\033[0m")
    print("OS Maker 1.0 — Python-powered mini operating system. Type 'help'.")
    try:
        while True:
            try:
                line = input(f"\033[1;32mguest@osmaker\033[0m:{os.cwd}$ ")
            except EOFError:
                break
            result = os.run(line)
            if result:
                print(result)
            SAVE_FILE.write_text(json.dumps(os.root, indent=2), encoding="utf-8")
    except SystemExit:
        pass
    finally:
        SAVE_FILE.write_text(json.dumps(os.root, indent=2), encoding="utf-8")
        print("OS Maker shut down. Your virtual disk has been saved.")


if __name__ == "__main__":
    main()
