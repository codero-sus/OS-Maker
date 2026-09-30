"""Unit tests for the OS Maker mini shell (:mod:`os_maker`).

Run from the repository root::

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

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


@contextlib.contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Swallow (and hand back) everything the shell prints."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


class DiskTests(unittest.TestCase):
    """The filesystem layer on its own: paths, contents and the save file."""

    def setUp(self) -> None:
        self.fs = os_maker.FileSystem()

    def test_paths_are_normalised_without_a_shell(self) -> None:
        self.assertEqual(self.fs.split("/etc/../var"), ["var"])
        self.assertEqual(self.fs.split("/"), [])
        self.assertEqual(self.fs.split("."), ["home", "guest"])  # relative to the cwd
        self.assertEqual(self.fs.absolute("/a/../b"), "/b")
        self.assertEqual(self.fs.path_of([]), "/")
        self.assertEqual(self.fs.path_of(["a", "b"]), "/a/b")

    def test_dollars_are_ordinary_characters_here(self) -> None:
        # expansion is the shell's job, so the disk must not reinterpret what it is given
        self.fs.write_file("/tmp/$HOME.txt", "kept")
        self.assertEqual(self.fs.split("/tmp/$HOME.txt"), ["tmp", "$HOME.txt"])
        self.assertEqual(self.fs.read_text("/tmp/$HOME.txt"), "kept")

    def test_chdir_moves_and_remembers_the_previous_directory(self) -> None:
        self.fs.chdir("/tmp")
        self.assertEqual((self.fs.cwd, self.fs.old_cwd), ("/tmp", "/home/guest"))
        self.fs.chdir("/")
        self.assertEqual(self.fs.cwd, "/")
        self.assertEqual(self.fs.split(".."), [])  # ".." above the root is the root itself

    def test_chdir_reports_why_it_refused(self) -> None:
        with self.assertRaises(NotADirectoryError) as caught:
            self.fs.chdir("/etc/hostname")
        self.assertIn("Not a directory", str(caught.exception))
        with self.assertRaises(FileNotFoundError):
            self.fs.chdir("/nope")

    def test_lookup_errors_name_the_offending_component(self) -> None:
        with self.assertRaises(NotADirectoryError) as caught:
            self.fs.resolve("/etc/hostname/passwd")
        self.assertIn("/etc/hostname", str(caught.exception))
        with self.assertRaises(FileNotFoundError) as caught:
            self.fs.resolve("/etc/nope/deeper")
        self.assertIn("/etc/nope", str(caught.exception))

    def test_the_root_directory_is_not_an_entry(self) -> None:
        with self.assertRaises(PermissionError) as caught:
            self.fs.resolve_parent("/")
        self.assertIn("cannot modify the root directory", str(caught.exception))

    def test_names_are_limited_to_255_bytes(self) -> None:
        with self.assertRaises(OSError) as caught:
            self.fs.touch("/tmp/" + "x" * (os_maker.MAX_NAME_LENGTH + 1))
        self.assertIn("File name too long", str(caught.exception))

    def test_touch_creates_then_updates(self) -> None:
        self.fs.touch("/tmp/late.txt")
        first = self.fs.resolve("/tmp/late.txt")["mtime"]
        self.fs.touch("/tmp/late.txt")
        self.assertGreaterEqual(self.fs.resolve("/tmp/late.txt")["mtime"], first)
        with self.assertRaises(IsADirectoryError):
            self.fs.touch("/etc")

    def test_read_and_write_refuse_the_wrong_kind_of_node(self) -> None:
        with self.assertRaises(IsADirectoryError):
            self.fs.read_text("/etc")
        self.fs.write_file("/tmp/plain.txt", "hi")
        with self.assertRaises(NotADirectoryError):
            self.fs.write_file("/tmp/plain.txt/deeper", "no")
        self.assertTrue(self.fs.is_directory("/etc"))
        self.assertFalse(self.fs.is_directory("/tmp/plain.txt"))

    def test_missing_parents_are_only_created_when_asked(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.fs.resolve_parent("/tmp/deep/deeper/file")
        self.assertFalse(self.fs.exists("/tmp/deep"))
        parent, leaf = self.fs.resolve_parent("/tmp/deep/deeper/file", create_parents=True)
        self.assertEqual(leaf, "file")
        self.assertEqual(parent["children"], {})  # the leaf itself is still for the caller to make
        self.assertTrue(self.fs.is_directory("/tmp/deep/deeper"))

    def test_remove_drops_an_entry(self) -> None:
        self.fs.write_file("/tmp/gone.txt", "x")
        self.fs.remove("/tmp/gone.txt")
        self.assertFalse(self.fs.exists("/tmp/gone.txt"))

    def test_is_inside_only_reports_strict_descendants(self) -> None:
        self.assertFalse(self.fs.is_inside("/tmp", "/tmp"))
        self.assertTrue(self.fs.is_inside("/tmp/sub", "/tmp"))
        self.assertTrue(self.fs.is_inside("/tmp/a/b", "/tmp/a"))
        self.assertFalse(self.fs.is_inside("/etc", "/tmp"))
        self.assertTrue(self.fs.exists("/tmp"))
        self.assertFalse(self.fs.exists("/tmp/nothing-here"))

    def test_saving_is_optional_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            fs = os_maker.FileSystem(save_file=disk_file)
            fs.save()  # nothing dirty yet: no write, no fuss
            self.assertFalse(disk_file.exists())
            fs.write_file("/home/guest/note.txt", "kept")
            fs.save()
            self.assertEqual({entry.name for entry in Path(tmp).iterdir()}, {"disk.json"})
            reloaded, problem = os_maker.FileSystem.open(disk_file)
            self.assertIsNone(problem)
            self.assertEqual(reloaded.read_text("/home/guest/note.txt"), "kept")

    def test_a_broken_disk_boots_anyway_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            disk_file.write_text("{ not json", encoding="utf-8")
            fs, problem = os_maker.FileSystem.open(disk_file)
            self.assertIn("is not a valid virtual disk", problem or "")
            self.assertEqual(fs.cwd, os_maker.HOME)
            fs.write_file("/tmp/x", "y")
            fs.save()  # saving repairs the disk, so the session is not wasted
            self.assertIn("y", disk_file.read_text(encoding="utf-8"))

    def test_an_unwritable_disk_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fs = os_maker.FileSystem(save_file=Path(tmp))  # a directory is not a file
            fs.write_file("/tmp/x", "y")
            self.assertIn("cannot save", fs.save() or "")
            self.assertEqual(fs.save_problems, 1)
            self.assertIn("cannot save", fs.save() or "")  # still reporting, still alive
            self.assertEqual(list(Path(tmp).iterdir()), [], "no half-written file is left behind")

    def test_open_without_a_file_starts_fresh(self) -> None:
        fs, problem = os_maker.FileSystem.open(None)
        self.assertIsNone(problem)
        self.assertTrue(fs.exists("/etc/os-release"))


class SessionTests(unittest.TestCase):
    """boot / feed / interact / shutdown, driven in-process so the tests see the code."""

    def test_boot_gives_a_clean_system(self) -> None:
        system, problem = os_maker.boot(save_file=None)
        self.assertIsNone(problem)
        self.assertEqual(system.hostname, "osmaker")
        self.assertEqual(system.cwd, os_maker.HOME)
        self.assertEqual(system.exit_code, 0)
        self.assertFalse(system.should_exit)
        self.assertIsNone(system.save_problems or None)

    def test_boot_reports_a_disk_it_cannot_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "disk.json"
            broken.mkdir()  # something is in the way where the disk should live
            system, problem = os_maker.boot(save_file=broken)
        self.assertIn("could not read", problem or "")
        self.assertEqual(system.run("echo still working"), "still working")

    def test_feed_runs_each_line_and_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            system, _ = os_maker.boot(save_file=disk_file)
            with quiet() as out:
                status = system.feed(["write /tmp/notes.txt hello", "cat /tmp/notes.txt"])
            self.assertEqual(status, 0)
            self.assertEqual(out.getvalue().strip(), "hello")
            reloaded, problem = os_maker.load_fs(disk_file)
            self.assertIsNone(problem)
            self.assertIn("hello", TinyOS(reloaded).read_text("/tmp/notes.txt"))

    def test_feed_stops_at_exit_and_keeps_its_status(self) -> None:
        system, _ = os_maker.boot()
        with quiet() as out:
            status = system.feed(["echo first", "exit 3", "echo never"])
        self.assertEqual(status, 3)
        self.assertEqual(out.getvalue().strip(), "first")

    def test_feed_reports_a_broken_command_without_raising(self) -> None:
        system, _ = os_maker.boot()
        with quiet() as out:
            system.feed(["nosuchcommand", "cat /etc/nope"])
        text = out.getvalue()
        self.assertIn("command not found", text)
        self.assertIn("No such file", text)

    def test_interact_reads_until_the_input_runs_out(self) -> None:
        answers = iter(["uname", "hostname myos"])
        system = TinyOS(read_line=lambda prompt: next(answers, None))
        with quiet() as out:
            self.assertEqual(system.interact(), 0)
        text = out.getvalue()
        self.assertIn("OSMaker", text)
        self.assertEqual(system.hostname, "myos")

    def test_interact_treats_a_none_line_as_end_of_input(self) -> None:
        system = TinyOS(read_line=lambda prompt: None)
        with quiet() as out:
            self.assertEqual(system.interact(), 0)
        self.assertEqual(out.getvalue(), "\n")  # end of input, cleanly

    def test_interact_recovers_from_ctrl_c(self) -> None:
        answers = iter([KeyboardInterrupt(), "echo alive"])

        def reader(_prompt: str) -> str | None:
            value = next(answers, None)  # exhausted input means end of session
            if isinstance(value, BaseException):
                raise value
            return value

        system = TinyOS(read_line=reader)
        with quiet() as out:
            self.assertEqual(system.interact(), 0)
        self.assertIn("^C", out.getvalue())
        self.assertIn("alive", out.getvalue())
        self.assertEqual(system.exit_code, 0)  # the line that ran afterwards reset it

    def test_the_shell_reads_input_by_default(self) -> None:
        system = TinyOS()  # no injected reader: the real input() is used
        with unittest.mock.patch("builtins.input", return_value="echo piped"):
            self.assertEqual(system.read_input("prompt"), "echo piped")

    def test_shutdown_says_what_happened_to_the_disk(self) -> None:
        system, _ = os_maker.boot(save_file=None)
        self.assertIn("nothing was saved", os_maker.shutdown(system))
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            system, _ = os_maker.boot(save_file=disk_file)
            system.run("write /keep.txt yes")
            self.assertIn("has been saved", os_maker.shutdown(system))
            self.assertTrue(disk_file.is_file())

            broken, _ = os_maker.boot(save_file=Path(tmp))  # a directory: unwritable
            broken.run("write /more.txt nope")
            with quiet():
                self.assertIn("could not be written", os_maker.shutdown(broken))

    def test_shutdown_leaves_a_disk_behind_after_a_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"  # does not exist yet: --reset just removed it
            system, _ = os_maker.boot(save_file=disk_file)
            self.assertIn("has been saved", os_maker.shutdown(system))
            self.assertIn("os-release", disk_file.read_text(encoding="utf-8"))

    def test_edit_uses_the_injected_reader(self) -> None:
        answers = iter(["one", "two", "."])
        system = TinyOS(read_line=lambda prompt: next(answers))
        self.assertEqual(system.run("edit /tmp/lines.txt"), "wrote 2 lines to /tmp/lines.txt")
        self.assertEqual(system.read_text("/tmp/lines.txt"), "one\ntwo\n")

    def test_edit_without_a_terminal_suggests_write(self) -> None:
        system = TinyOS()
        system.read_line = None
        self.assertEqual(
            system.run("edit /tmp/x"), "edit: no terminal to read from (use 'write <file> <text>' instead)"
        )

    def test_edit_starts_from_the_existing_lines(self) -> None:
        system = TinyOS()
        system.run("write /tmp/notes.txt old line")
        answers = iter(["fresh", None])
        system.read_line = lambda prompt: next(answers)
        self.assertIn("wrote 2 lines", system.run("edit /tmp/notes.txt"))
        self.assertEqual(system.read_text("/tmp/notes.txt"), "old line\nfresh\n")

    def test_the_banner_names_the_version_the_user_and_the_place(self) -> None:
        system, _ = os_maker.boot()
        text = os_maker.banner(system)
        self.assertIn("M A K E R", text)
        self.assertIn(os_maker.VERSION, text)
        self.assertIn("guest@osmaker", text)
        self.assertIn("cwd ~", text)
        self.assertIn("Type 'help'", text)
        system.unicode_output = False
        self.assertIn("O S   M A K E R", os_maker.banner(system))

    def test_prompt_shows_the_working_directory(self) -> None:
        system, _ = os_maker.boot()
        self.assertIn("guest@osmaker", os_maker.prompt_for(system))
        self.assertTrue(os_maker.prompt_for(system).endswith(":~$ "))
        system.run("cd /etc")
        self.assertIn("guest@osmaker:/etc$ ", os_maker.prompt_for(system))
        self.assertEqual(os_maker.display_cwd(system), "/etc")

    def test_unicode_support_follows_the_stream_encoding(self) -> None:
        for encoding, expected in (
            ("utf-8", True),
            ("UTF-8", True),
            ("ascii", False),
            ("", False),
            ("nope-9", False),
        ):
            self.assertEqual(
                os_maker.supports_unicode(SimpleNamespace(encoding=encoding)), expected, encoding
            )

    def test_unknown_and_malformed_lines_set_the_status(self) -> None:
        system, _ = os_maker.boot()
        with quiet():
            system.run("frobnicate")
            self.assertEqual(system.exit_code, 127)
            system.run("echo 'unbalanced")
            self.assertEqual(system.exit_code, 2)
            system.run("cat /etc/missing")
            self.assertEqual(system.exit_code, 1)
            self.assertEqual(system.run("# just a comment"), "")
            self.assertEqual(system.exit_code, 1)  # a comment runs nothing, so $? survives

    def test_flags_are_strict_about_options(self) -> None:
        system, _ = os_maker.boot()
        self.assertEqual(system.run("ls -Q"), "ls: invalid option -- 'Q'")
        self.assertEqual(system.run("ls --colour"), "ls: unrecognized option '--colour'")
        self.assertEqual(system.run("du /etc /var"), "du: extra operand '/var' (see 'man du')")

    def test_history_records_what_was_run(self) -> None:
        system, _ = os_maker.boot()
        with quiet():
            system.run("echo hi")
            system.run("   ")  # blank lines are not history
        self.assertEqual(system.history, ["echo hi"])

    def test_main_can_be_driven_without_a_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            with quiet() as first:
                self.assertEqual(
                    os_maker.main(["--no-banner", "--save-file", str(disk_file), "-e", "write /m.txt hi"]), 0
                )
            self.assertIn("has been saved", first.getvalue())
            with quiet() as second:
                self.assertEqual(
                    os_maker.main(["--no-banner", "--save-file", str(disk_file), "-e", "cat /m.txt"]), 0
                )
            self.assertIn("hi", second.getvalue())
            with quiet() as reset:
                self.assertEqual(
                    os_maker.main(
                        ["--no-banner", "--reset", "--save-file", str(disk_file), "-e", "cat /m.txt"]
                    ),
                    1,  # the disk was discarded, so the shell reports the missing file
                )
            self.assertIn("No such file", reset.getvalue())

    def test_main_passes_a_command_status_through(self) -> None:
        with quiet():
            self.assertEqual(os_maker.main(["--no-banner", "--no-save", "-e", "exit 7"]), 7)

    def test_main_warns_about_a_disk_it_cannot_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, quiet() as out:
            self.assertEqual(os_maker.main(["--no-banner", "--save-file", tmp, "-e", "touch /x"]), 0)
        self.assertIn("could not be written", out.getvalue())

    def test_parser_defaults(self) -> None:
        args = os_maker.build_parser().parse_args([])
        self.assertEqual(args.execute, [])
        self.assertFalse(args.no_save)
        self.assertFalse(args.reset)
        self.assertFalse(args.no_banner)
        self.assertFalse(args.ascii)
        self.assertIsNone(args.save_file)
        with self.assertRaises(SystemExit):
            os_maker.build_parser().parse_args(["--version"])

    def test_readline_is_optional_but_never_breaks_a_session(self) -> None:
        try:
            import readline
        except ImportError:
            self.skipTest("no readline on this platform")
        system, _ = os_maker.boot()
        system.run("echo seeded")
        with quiet():
            saver = os_maker.setup_readline(system, persist_history=False)
        self.addCleanup(readline.set_completer, None)
        completer = readline.get_completer()
        self.assertIsNotNone(completer)
        self.assertTrue(completer("ca", 0).startswith("cat"))
        if saver is not None:
            saver()  # persist_history=False must not write anything to the home directory


class EdgeCaseTests(unittest.TestCase):
    """The odd corners: malformed disks, quoting, colours and a hostile filesystem."""

    def test_a_malformed_disk_is_rejected_with_a_useful_reason(self) -> None:
        cases = [
            ({"type": "dir"}, "directory without a children map"),
            ({"type": "file", "content": 7}, "file content must be a string"),
            ({"type": "symlink"}, "unknown node type"),
            ({"type": "dir", "children": {"": {}}}, "invalid entry name"),
            ({"type": "dir", "children": {"a/b": {}}}, "invalid entry name"),
            ({"type": "dir", "children": {"x" * 300: {}}}, "entry name too long"),
            ({"type": "dir", "children": {"a": {"type": "bogus"}}}, "unknown node type"),
        ]
        for root, fragment in cases:
            with self.subTest(root=str(root)[:40]):
                with self.assertRaises(ValueError) as caught:
                    os_maker.validate_fs(root)
                self.assertIn(fragment, str(caught.exception))

    def test_load_fs_reports_a_tree_it_cannot_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            disk_file.write_text('{"type": "dir", "children": {"a": {}}}', encoding="utf-8")
            root, problem = os_maker.load_fs(disk_file)
            self.assertIsNotNone(problem)
            self.assertEqual(root["type"], "dir")  # a usable disk was handed back anyway

    def test_quoting_and_escapes(self) -> None:
        self.assertEqual(
            [token.value for token in os_maker.lex("echo \"a b\" 'c $d' e\\ f")],
            ["echo", "a b", "c $d", "e f"],
        )
        self.assertEqual([token.value for token in os_maker.lex('echo "say \\"hi\\""')], ["echo", 'say "hi"'])
        with self.assertRaises(os_maker.ShellSyntaxError):
            os_maker.lex("echo 'unterminated")
        self.assertIsNone(parse(os_maker.lex("")))

    def test_the_hostname_only_sticks_if_it_is_legal(self) -> None:
        for stored, expected in (("desk-01", "desk-01"), ("Not Legal", "osmaker"), ("", "osmaker")):
            with self.subTest(stored=stored):
                root = os_maker.fresh_fs()
                root["children"]["etc"]["children"]["hostname"] = os_maker.make_file(stored + "\n")
                if not stored:
                    del root["children"]["etc"]["children"]["hostname"]
                self.assertEqual(TinyOS(root).hostname, expected)

    def test_the_message_of_the_day_shows_up_in_the_banner(self) -> None:
        system = TinyOS()
        system.run("write /etc/motd Read the manual first.")
        self.assertIn("Read the manual first.", os_maker.banner(system))

    def test_the_prompt_is_coloured_on_a_terminal(self) -> None:
        system = TinyOS()
        system.unicode_output = True
        with (
            unittest.mock.patch("sys.stdout.isatty", return_value=True),
            unittest.mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIn("\033[1;32m", os_maker.prompt_for(system))
        with (
            unittest.mock.patch("sys.stdout.isatty", return_value=True),
            unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"}),
        ):
            self.assertNotIn("\033", os_maker.prompt_for(system))

    def test_display_cwd_falls_back_to_the_absolute_path(self) -> None:
        system = TinyOS()
        system.run("cd /var")
        self.assertEqual(os_maker.display_cwd(system), "/var")

    def test_main_gives_up_when_the_disk_cannot_be_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk_file = Path(tmp) / "disk.json"
            disk_file.write_text("{}", encoding="utf-8")
            with (
                unittest.mock.patch.object(Path, "unlink", side_effect=OSError("busy")),
                quiet() as out,
            ):
                self.assertEqual(
                    os_maker.main(["--reset", "--save-file", str(disk_file), "-e", "echo hi"]), 1
                )
        self.assertIn("could not reset", out.getvalue())

    def test_main_sets_up_line_editing_only_for_a_terminal(self) -> None:
        calls: list[bool] = []

        def spy(_system: TinyOS, *, persist_history: bool) -> None:
            calls.append(persist_history)

        system = TinyOS(read_line=lambda _prompt: None)
        with (
            unittest.mock.patch.object(os_maker, "setup_readline", spy),
            unittest.mock.patch("sys.stdin.isatty", return_value=True),
            unittest.mock.patch.object(os_maker, "boot", return_value=(system, None)),
            quiet(),
        ):
            self.assertEqual(os_maker.main(["--no-banner", "--no-save"]), 0)
        self.assertEqual(calls, [False])  # interactive, and --no-save keeps the history file alone

        calls.clear()
        with (
            unittest.mock.patch.object(os_maker, "setup_readline", spy),
            unittest.mock.patch("sys.stdin.isatty", return_value=False),
            quiet(),
        ):
            self.assertEqual(os_maker.main(["--no-banner", "--no-save", "-e", "echo hi"]), 0)
        self.assertEqual(calls, [])  # a piped session never touches readline


class UsageTests(unittest.TestCase):
    """The contracts each built-in keeps when it is called wrongly."""

    def setUp(self) -> None:
        self.system = TinyOS()

    def say(self, line: str) -> str:
        return self.system.run(line)

    def test_missing_operands_are_named(self) -> None:
        cases = [
            ("mkdir", "mkdir: missing operand"),
            ("rmdir", "rmdir: missing operand"),
            ("touch", "touch: missing operand"),
            ("write", "write: needs a file and the text to store"),
            ("append /tmp/x", "append: needs a file and the text to add"),
            ("cat", "cat: missing file operand"),
            ("rm", "rm: missing operand"),
            ("cp /tmp/a", "cp: needs at least a source and a destination"),
            ("mv /tmp/a", "mv: needs at least a source and a destination"),
            ("stat", "stat: missing operand"),
            ("edit", "edit: missing file operand"),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(self.say(line), expected)

    def test_too_many_operands_are_refused(self) -> None:
        self.assertEqual(self.say("pwd /tmp"), "pwd: extra operand '/tmp' (see 'man pwd')")
        self.assertEqual(self.say("uname -x"), "uname: invalid option '-x'")
        self.assertEqual(self.say("date now"), "date: extra operand 'now' (see 'man date')")

    def test_cd_needs_a_previous_directory(self) -> None:
        self.assertEqual(self.say("cd -"), "cd: OLDPWD not set")
        self.assertEqual(self.say("cd /tmp"), "")
        self.assertEqual(self.say("cd -"), "")
        self.assertEqual(self.system.cwd, os_maker.HOME)

    def test_mkdir_reports_collisions_and_accepts_dash_p(self) -> None:
        self.assertEqual(self.say("mkdir /tmp/one"), "")
        self.assertEqual(
            self.say("mkdir /tmp/one"), "mkdir: /tmp/one: cannot create directory (already exists)"
        )
        self.assertEqual(self.say("mkdir -p /tmp/one"), "")  # -p never complains about an existing dir
        self.assertEqual(self.say("mkdir -p /tmp/a/b/c"), "")
        self.assertTrue(self.system.exists("/tmp/a/b/c"))
        self.assertEqual(self.say("mkdir /nowhere/one"), "mkdir: /nowhere: No such file or directory")

    def test_rmdir_is_pickier_than_rm(self) -> None:
        self.assertEqual(self.say("rmdir /etc/hostname"), "rmdir: /etc/hostname: Not a directory")
        self.assertEqual(self.say("mkdir /tmp/full"), "")
        self.assertEqual(self.say("touch /tmp/full/x"), "")
        self.assertEqual(self.say("rmdir /tmp/full"), "rmdir: /tmp/full: Directory not empty")
        self.assertEqual(self.say("rm -r /tmp/full"), "")
        self.assertEqual(self.say("rmdir /tmp/full"), "rmdir: /tmp/full: No such file or directory")
        self.assertEqual(self.say("rmdir /"), "rmdir: cannot remove '/'")

    def test_double_dash_ends_the_flags(self) -> None:
        self.assertEqual(self.say("ls -- /tmp"), self.say("ls /tmp"))
        self.assertEqual(self.say("cat -- /etc/hostname"), self.say("cat /etc/hostname"))
        self.assertEqual(self.say("touch -x /tmp/a"), "touch: invalid option -- 'x'")
        self.assertEqual(self.say("stat -x /etc"), "stat: invalid option -- 'x'")

    def test_help_and_man_agree(self) -> None:
        self.assertEqual(self.say("help ls"), self.say("man ls"))
        self.assertEqual(self.say("man frobnicate"), "man: no manual entry for frobnicate")

    def test_append_creates_then_extends(self) -> None:
        self.assertEqual(self.say("append /tmp/log.txt first"), "")
        self.assertEqual(self.say("append /tmp/log.txt second"), "")
        self.assertEqual(self.system.read_text("/tmp/log.txt"), "first\nsecond")

    def test_a_dirty_disk_reports_itself_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            system = TinyOS(save_file=Path(tmp))  # a directory: never writable
            with quiet() as out:
                system.run("touch /a.txt")
                self.assertTrue(system.dirty)
                system.save()
                system.save()
                system.save()
            self.assertEqual(out.getvalue().count("cannot save"), 1)
            self.assertEqual(system.save_problems, 3)
            self.assertTrue(system.dirty)  # nothing was written, so nothing was cleared

    def test_readline_keeps_a_history_file_when_asked(self) -> None:
        try:
            import readline
        except ImportError:
            self.skipTest("no readline on this platform")
        if "libedit" in (readline.__doc__ or ""):
            self.skipTest("macOS ships libedit, whose history file format differs")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "os-maker" / "history").parent.mkdir()
            (state / "os-maker" / "history").write_text("earlier command\n", encoding="utf-8")
            system = TinyOS()
            with (
                unittest.mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
                quiet(),
            ):
                saver = os_maker.setup_readline(system, persist_history=True)
                self.assertIsNotNone(saver)
                self.addCleanup(readline.set_completer, None)
                system.run("echo hello")
                readline.add_history("echo hello")
                saver()  # type: ignore[misc]
            self.assertIn("echo hello", (state / "os-maker" / "history").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
