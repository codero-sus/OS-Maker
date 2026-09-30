"""Unit tests for the OS Maker mini shell (:mod:`os_maker`).

Run from the repository root::

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os_maker
from os_maker import ParsedLine, ShellSyntaxError, TinyOS, lex, parse


def shell(**kwargs: object) -> TinyOS:
    """A TinyOS with a throwaway filesystem and no disk file."""
    system = TinyOS(os_maker.fresh_fs(), save_file=None)
    system.unicode_output = bool(kwargs.get("unicode", True))
    return system


class LexingTests(unittest.TestCase):
    def test_plain_words(self) -> None:
        self.assertEqual([token.value for token in lex("ls -l /tmp")], ["ls", "-l", "/tmp"])

    def test_quotes_keep_spaces_and_do_not_escape(self) -> None:
        tokens = lex("write a.txt \"two  words\" 'raw $HOME'")
        self.assertEqual([token.value for token in tokens], ["write", "a.txt", "two  words", "raw $HOME"])
        self.assertTrue(tokens[3].quoted)
        self.assertFalse(tokens[2].quoted)

    def test_backslash_escapes(self) -> None:
        self.assertEqual(lex(r"echo a\ b")[0].value, "echo")
        self.assertEqual(lex(r"echo a\ b")[1].value, "a b")

    def test_redirection_operators(self) -> None:
        self.assertEqual(
            [token.value for token in lex("echo hi >> log.txt")], ["echo", "hi", ">>", "log.txt"]
        )
        self.assertTrue(lex("echo hi > log.txt")[2].operator)

    def test_unbalanced_quote_is_reported(self) -> None:
        with self.assertRaises(ShellSyntaxError):
            lex('echo "unfinished')

    def test_unsupported_operators_are_rejected(self) -> None:
        for line in ("ls | wc -l", "cd / && pwd", "false &", "echo a < b"):
            with self.subTest(line=line), self.assertRaises(ShellSyntaxError):
                lex(line)

    def test_parse_without_redirection(self) -> None:
        self.assertEqual(parse(lex("pwd")), ParsedLine("pwd", []))

    def test_parse_redirection(self) -> None:
        self.assertEqual(parse(lex("ls > out")), ParsedLine("ls", [], ">", "out"))

    def test_parse_rejects_two_redirections_and_missing_target(self) -> None:
        with self.assertRaises(ShellSyntaxError):
            parse(lex("ls > a > b"))
        with self.assertRaises(ShellSyntaxError):
            parse(lex("echo hi >"))
        with self.assertRaises(ShellSyntaxError):
            parse(lex("> somewhere"))


class PathTests(unittest.TestCase):
    def test_relative_absolute_and_dotdot(self) -> None:
        system = shell()
        self.assertEqual(system.split("../../../etc"), ["etc"])
        self.assertEqual(system.split("/etc/../var"), ["/var".strip("/")])
        self.assertEqual(system.split("/"), [])
        self.assertEqual(system.absolute("./x"), "/home/guest/x")

    def test_home_and_variables_expand(self) -> None:
        system = shell()
        self.assertEqual(system.split("~/notes.txt"), ["home", "guest", "notes.txt"])
        self.assertEqual(system.split("$HOME/x"), ["home", "guest", "x"])
        self.assertEqual(system.split("${PWD}/x"), ["home", "guest", "x"])
        self.assertEqual(system.split("$NOPE/x"), ["x"])  # unset variables vanish, as in sh

    def test_single_quotes_do_not_expand(self) -> None:
        system = shell()
        tokens = lex("'~'")
        self.assertEqual(system.expand(tokens[0].value, quoted=tokens[0].quoted), "~")

    def test_dotdot_cannot_escape_the_root(self) -> None:
        system = shell()
        self.assertEqual(system.split("/../../etc"), ["etc"])
        self.assertEqual(system.split(".."), ["home"])  # relative to /home/guest
        self.assertEqual(system.split("/.."), [])  # ".." above the root is the root itself
        self.assertEqual(system.resolve("/..")["type"], "dir")
        self.assertEqual(system.run("cd /home/guest/../.."), "")
        self.assertEqual(system.run("pwd"), "/")

    def test_missing_file_message(self) -> None:
        system = shell()
        self.assertEqual(system.run("cat /nope"), "cat: /nope: No such file or directory")
        self.assertEqual(system.exit_code, 1)


class FileSystemTests(unittest.TestCase):
    def test_seed_files_exist(self) -> None:
        system = shell()
        self.assertIn("OS Maker", system.read_text("/etc/os-release"))
        self.assertEqual(system.hostname, "osmaker")
        self.assertTrue(system.exists("/home/guest/welcome.txt"))

    def test_mkdir_touch_ls_cat(self) -> None:
        system = shell()
        self.assertEqual(system.run("cd /tmp"), "")
        self.assertEqual(system.cwd, "/tmp")
        self.assertEqual(system.run("mkdir work"), "")
        self.assertEqual(system.run("touch work/empty.txt"), "")
        self.assertIn("work/", system.run("ls"))
        self.assertEqual(system.run("write work/file.txt hello there"), "")
        self.assertEqual(system.run("cat work/file.txt"), "hello there")
        self.assertEqual(system.run("append work/file.txt again"), "")
        self.assertEqual(system.run("cat work/file.txt"), "hello there\nagain")

    def test_mkdir_needs_parent_without_p(self) -> None:
        system = shell()
        self.assertEqual(system.run("mkdir /tmp/a/b"), "mkdir: /tmp/a: No such file or directory")
        self.assertEqual(system.run("mkdir -p /tmp/a/b"), "")
        self.assertTrue(system.exists("/tmp/a/b"))

    def test_rm_semantics(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/dir/inner")
        self.assertEqual(system.run("rm /tmp/dir"), "rm: /tmp/dir: Directory not empty (use -r)")
        self.assertEqual(system.run("rm -r /tmp/dir"), "")
        self.assertFalse(system.exists("/tmp/dir"))
        self.assertEqual(system.run("rm /tmp/missing"), "rm: /tmp/missing: No such file or directory")
        self.assertEqual(system.run("rm -f /tmp/missing"), "")
        self.assertEqual(system.run("rm /"), "rm: cannot remove '/': that is the whole virtual disk")

    def test_cp_and_mv(self) -> None:
        system = shell()
        system.run("write /tmp/a.txt content")
        self.assertEqual(system.run("cp /tmp/a.txt /tmp/b.txt"), "")
        self.assertEqual(system.run("cat /tmp/b.txt"), "content")
        self.assertEqual(system.run("mv /tmp/b.txt /tmp/c.txt"), "")
        self.assertFalse(system.exists("/tmp/b.txt"))
        self.assertEqual(
            system.run("cp /tmp/a.txt /tmp/a.txt"), "cp: '/tmp/a.txt' and '/tmp/a.txt' are the same file"
        )
        self.assertEqual(system.run("mkdir -p /tmp/dst"), "")
        self.assertEqual(system.run("cp /tmp/a.txt /tmp/dst"), "")
        self.assertTrue(system.exists("/tmp/dst/a.txt"))
        self.assertIn("omitting directory", system.run("cp /tmp/dst /tmp/e"))
        # 'dst' does not exist yet, so cp -r creates it as a copy (like real cp) ...
        self.assertEqual(system.run("cp -r /tmp/dst /tmp/e"), "")
        self.assertTrue(system.exists("/tmp/e/a.txt"))
        # ... unless the destination already is a directory, and then it lands inside it
        self.assertEqual(system.run("cp -r /tmp/dst /tmp/e"), "")
        self.assertTrue(system.exists("/tmp/e/dst/a.txt"))

    def test_mv_rejects_moving_a_directory_into_itself(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/outer/inner")
        self.assertIn("subdirectory of itself", system.run("mv /tmp/outer /tmp/outer/inner"))

    def test_copy_is_deep(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/src /tmp/dst")
        system.run("write /tmp/src/f.txt one")
        system.run("cp -r /tmp/src /tmp/dst")  # /tmp/dst exists, so src lands inside it
        system.run("write /tmp/src/f.txt two")
        self.assertEqual(system.run("cat /tmp/dst/src/f.txt"), "one")

    def test_write_into_directory_is_refused(self) -> None:
        system = shell()
        self.assertEqual(system.run("write /etc /nope"), "write: /etc: Is a directory")

    def test_cd_dash_and_pwd(self) -> None:
        system = shell()
        system.run("cd /tmp")
        system.run("cd /var")
        self.assertEqual(system.run("cd -"), "")
        self.assertEqual(system.run("pwd"), "/tmp")
        system.run("cd")
        self.assertEqual(system.run("pwd"), os_maker.HOME)
        self.assertEqual(system.run("cd -"), "")
        self.assertEqual(system.run("pwd"), "/tmp")
        self.assertIn("extra operand", system.run("cd /etc /var"))

    def test_navigation_errors(self) -> None:
        system = shell()
        self.assertEqual(system.run("cd /etc/os-release"), "cd: /etc/os-release: Not a directory")
        self.assertEqual(system.run("cd /nowhere"), "cd: /nowhere: No such file or directory")

    def test_listing_of_a_file_and_empty_dir(self) -> None:
        system = shell()
        self.assertTrue(system.run("ls /etc/os-release").endswith("B"))
        self.assertEqual(system.run("ls /bin"), "(empty)")

    def test_hidden_files_need_a_flag(self) -> None:
        system = shell()
        system.run("touch /tmp/.secret")
        self.assertNotIn(".secret", system.run("ls /tmp"))
        self.assertIn(".secret", system.run("ls -a /tmp"))

    def test_long_listing_reports_size(self) -> None:
        system = shell()
        system.run("write /tmp/x.txt 12345")
        listing = system.run("ls -l /tmp")
        self.assertIn("-rw-r--r--", listing)
        self.assertIn("x.txt", listing)
        self.assertIn("total", listing)

    def test_tree_indents_nested_entries(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/p/ideas /tmp/p/other")
        system.run("touch /tmp/p/ideas/a /tmp/p/other/b")
        system.unicode_output = False
        tree = system.run("tree /tmp").splitlines()
        self.assertEqual(
            tree[:6],
            ["/tmp", "`-- p/", "    |-- ideas/", "    |   `-- a", "    `-- other/", "        `-- b"],
        )

    def test_tree_and_stat_and_du(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/a/b")
        system.run("touch /tmp/a/file")
        tree = system.run("tree /tmp")
        self.assertIn("a/", tree)
        self.assertIn("b/", tree)
        self.assertIn("2 directories, 1 file", tree)
        self.assertIn("regular file", system.run("stat /tmp/a/file"))
        self.assertIn("directory", system.run("stat /tmp/a"))
        self.assertIn("/tmp/a", system.run("du /tmp"))

    def test_df_reports_usage(self) -> None:
        out = shell().run("df")
        self.assertIn("/dev/osda1", out)
        self.assertIn("Mounted on", out)

    def test_redirection_truncates_and_appends(self) -> None:
        system = shell()
        system.run("echo one > /tmp/n.txt")
        self.assertEqual(system.run("cat /tmp/n.txt"), "one")
        system.run("echo two >> /tmp/n.txt")
        self.assertEqual(system.run("cat /tmp/n.txt"), "one\ntwo")
        system.run("echo three > /tmp/n.txt")
        self.assertEqual(system.run("cat /tmp/n.txt"), "three")

    def test_redirection_into_existing_directory(self) -> None:
        system = shell()
        self.assertEqual(system.run("ls > /etc"), "ls: /etc: Is a directory")

    def test_unknown_command_and_syntax_error_exit_codes(self) -> None:
        system = shell()
        self.assertEqual(system.run("frobnicate"), "frobnicate: command not found (try 'help')")
        self.assertEqual(system.exit_code, 127)
        self.assertIn("syntax error", system.run("echo 'unbalanced"))
        self.assertEqual(system.exit_code, 2)

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        system = shell()
        self.assertEqual(system.run("# just a note"), "")
        self.assertEqual(system.run("   "), "")
        self.assertEqual(system.history, [])

    def test_alias_expansion(self) -> None:
        system = shell()
        system.run("touch /tmp/z.txt")
        self.assertIn("-rw-r--r--", system.run("ll /tmp"))
        self.assertEqual(system.run("dir /bin"), "(empty)")

    def test_hostname_command_updates_file_and_prompt(self) -> None:
        system = shell()
        self.assertEqual(system.run("hostname study-box"), "")
        self.assertEqual(system.run("hostname"), "study-box")
        self.assertIn("study-box", system.read_text("/etc/hostname"))
        self.assertEqual(
            system.run("hostname 'bad name!'"),
            "hostname: 'bad name!' is not a valid hostname (letters, digits and '-')",
        )

    def test_status_variable(self) -> None:
        system = shell()
        system.run("cd /does/not/exist")
        self.assertEqual(system.run("echo status=$?"), "status=1")
        system.run("pwd")
        self.assertEqual(system.run("echo status=$?"), "status=0")

    def test_info_commands(self) -> None:
        system = shell()
        self.assertEqual(system.run("whoami"), os_maker.USER)
        self.assertIn("OSMaker", system.run("uname -a"))
        self.assertIn("PATH=", system.run("env"))
        self.assertIn("up 0:", system.run("uptime"))
        self.assertGreaterEqual(len(system.run("date").split()), 5)
        history = system.run("history")
        self.assertIn("date", history)  # the history command itself is listed too
        self.assertTrue(history.strip().splitlines()[-1].endswith("history"))

    def test_help_documents_every_command(self) -> None:
        system = shell()
        help_text = system.run("help")
        for name in os_maker.COMMANDS:
            self.assertIn(name, help_text)
        for name, info in os_maker.COMMANDS.items():
            self.assertTrue(info.usage, f"{name} has no usage line")
            self.assertTrue(info.summary, f"{name} has no summary")
        for alias in os_maker.ALIASES.values():
            self.assertIn(alias.split()[0], os_maker.COMMANDS, "alias points at a missing command")

    def test_man_page_and_unknown_topic(self) -> None:
        system = shell()
        self.assertIn("Usage: ls [-l] [-a] [path...]", system.run("man ls"))
        self.assertEqual(system.run("man nothing"), "man: no manual entry for nothing")
        self.assertEqual(system.run("man"), "man: what manual page do you want?")

    def test_extra_operand_is_rejected(self) -> None:
        system = shell()
        self.assertIn("extra operand", system.run("pwd now"))

    def test_edit_uses_the_injected_reader(self) -> None:
        lines = iter(["first line", "second line", "."])
        system = shell()
        system.read_line = lambda prompt: next(lines)
        output = system.run("edit /tmp/notes.md")
        self.assertIn("2 lines", output)
        self.assertEqual(system.read_text("/tmp/notes.md"), "first line\nsecond line\n")

    def test_edit_without_a_terminal(self) -> None:
        system = shell()
        self.assertEqual(
            system.run("edit /tmp/x"), "edit: no terminal to read from (use 'write <file> <text>' instead)"
        )

    def test_exit_sets_the_flag_and_status(self) -> None:
        system = shell()
        system.run("exit 3")
        self.assertTrue(system.should_exit)
        self.assertEqual(system.exit_code, 3)

    def test_clear_escape_only_on_a_tty(self) -> None:
        system = shell()
        self.assertEqual(system.run("clear"), "[screen cleared]")


class FakeReadline:
    """Enough of the readline API to exercise the completer."""

    def __init__(self, buffer: str) -> None:
        self.buffer = buffer

    def get_line_buffer(self) -> str:
        return self.buffer

    @property
    def word_start(self) -> int:
        return self.buffer.rfind(" ") + 1  # readline marks the start of the word being completed

    def get_begidx(self) -> int:
        return self.word_start

    def get_endidx(self) -> int:
        return len(self.buffer)


class CompletionTests(unittest.TestCase):
    def collect(self, system: TinyOS, buffer: str) -> list[str]:
        """Ask the completer the way readline does: the word being completed, then 0, 1, 2..."""
        readline = FakeReadline(buffer)
        complete = os_maker.make_completer(readline, system)
        text = buffer[readline.get_begidx() :]
        found, state = [], 0
        while True:
            item = complete(text, state)
            if item is None:
                return found
            found.append(item)
            state += 1

    def test_command_names_are_completed(self) -> None:
        system = shell()
        matches = self.collect(system, "mk")
        self.assertEqual(matches, ["mkdir "])

    def test_path_completion_marks_directories(self) -> None:
        system = shell()
        system.run("mkdir -p /tmp/build")
        system.run("touch /tmp/build/out.txt")
        system.run("cd /tmp")
        matches = self.collect(system, "cd bu")
        self.assertEqual(matches, ["build/"])
        matches = self.collect(system, "cat build/o")
        self.assertEqual(matches, ["build/out.txt "])

    def test_completion_on_broken_paths_is_silent(self) -> None:
        system = shell()
        self.assertEqual(self.collect(system, "ls /does/not/exist/x"), [])

    def test_tilde_completion(self) -> None:
        system = shell()
        system.run("mkdir -p ~/Documents")
        self.assertEqual(self.collect(system, "cd ~/Doc"), ["~/Documents/"])


class PersistenceTests(unittest.TestCase):
    def test_round_trip_through_the_save_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            system = TinyOS(os_maker.fresh_fs(), save_file=save)
            system.run("write /home/guest/kept.txt still here")
            system.save()
            self.assertTrue(save.is_file())
            self.assertFalse(list(Path(tmp).glob("*.tmp")))  # atomic replace, no leftovers
            reloaded, problem = os_maker.load_fs(save)
            self.assertIsNone(problem)
            self.assertEqual(TinyOS(reloaded, save_file=save).read_text("/home/guest/kept.txt"), "still here")

    def test_save_is_skipped_when_nothing_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            system = TinyOS(os_maker.fresh_fs(), save_file=save)
            system.save()
            self.assertFalse(save.exists())

    def test_corrupt_disk_falls_back_to_a_fresh_filesystem(self) -> None:
        for payload in (
            "not json at all",
            '{"type": "dir", "children": {"x": {"type": "spacesuit"}}}',
            '["a", "list"]',
        ):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(payload=payload):
                save = Path(tmp) / "disk.json"
                save.write_text(payload, encoding="utf-8")
                root, problem = os_maker.load_fs(save)
                self.assertIsNotNone(problem, f"{payload!r} should be rejected")
                os_maker.validate_fs(root)

    def test_missing_disk_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, problem = os_maker.load_fs(Path(tmp) / "absent.json")
            self.assertIsNone(problem)
            self.assertEqual(root["type"], "dir")

    def test_validate_fs_rejects_bad_names(self) -> None:
        with self.assertRaises(ValueError):
            os_maker.validate_fs({"type": "dir", "children": {"a/b": {"type": "file", "content": ""}}})


class CommandLineTests(unittest.TestCase):
    """End-to-end checks through ``python3 os_maker.py``."""

    def run_cli(self, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(Path(os_maker.__file__)), *argv],
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=60,
            input="",
        )

    def test_execute_then_reload_the_saved_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            first = self.run_cli(
                "--save-file", str(save), "-e", "write /home/guest/note.txt persisted", "-e", "exit"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("saved", first.stdout)
            second = self.run_cli("--save-file", str(save), "-e", "cat /home/guest/note.txt")
            self.assertIn("persisted", second.stdout)

    def test_reset_discards_the_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            self.run_cli("--save-file", str(save), "-e", "touch /home/guest/gone.txt")
            self.assertTrue(save.is_file())
            out = self.run_cli("--save-file", str(save), "--reset", "-e", "ls /home/guest")
            self.assertNotIn("gone.txt", out.stdout)

    def test_no_save_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            result = self.run_cli("--save-file", str(save), "--no-save", "-e", "touch /home/guest/x")
            self.assertIn("nothing was saved", result.stdout)
            self.assertFalse(save.exists())

    def test_version_and_banner(self) -> None:
        result = self.run_cli("--version")
        self.assertEqual(result.stdout.strip().split()[-1], os_maker.VERSION)
        booted = self.run_cli("--no-save", "-e", "pwd")
        self.assertIn("M A K E R", booted.stdout)
        self.assertNotIn("kona", booted.stdout)

    def test_unwritable_disk_warns_instead_of_crashing(self) -> None:
        # A path that is a directory cannot be replaced by the save file.
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "disk.json"
            blocked.mkdir()
            result = self.run_cli("--save-file", str(blocked), "-e", "touch /home/guest/x")
            self.assertIn("could not be written", result.stdout + result.stderr)
            self.assertFalse((Path(tmp) / "disk.json.tmp").exists(), "the temp file is cleaned up")

    def test_exit_code_is_propagated(self) -> None:
        result = self.run_cli("--no-save", "-e", "exit 7")
        self.assertEqual(result.returncode, 7)

    def test_interactive_session_reads_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "disk.json"
            completed = subprocess.run(
                [sys.executable, str(Path(os_maker.__file__)), "--save-file", str(save), "--no-banner"],
                input="echo hi > /tmp/stdin.txt\ncat /tmp/stdin.txt\nexit\n",
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("hi", completed.stdout)


if __name__ == "__main__":
    unittest.main()
