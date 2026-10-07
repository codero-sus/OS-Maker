"""Tests for the graphical builder front-end (:mod:`os_builder` and :mod:`os_builder_gui`).

Nothing here needs a display.  The pure layers (recipe, validation, branding, argv,
progress) are exercised directly, the headless CLI modes run end to end, and the Tk
view is driven through a small fake ``tkinter`` so widget wiring, log colouring and the
polling loop are covered too.

Run from the repository root::

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os_builder


class Captured:
    """stdout and stderr captured together, because the builder writes to both."""

    def __init__(self, out: io.StringIO, err: io.StringIO) -> None:
        self.out, self.err = out, err

    @property
    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()


@contextlib.contextmanager
def captured() -> Iterator[Captured]:
    """Capture stdout and stderr; ``text`` is both of them combined."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield Captured(out, err)


class TempDirCase(unittest.TestCase):
    """A scratch directory per test, plus a recipe pointed at it."""

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.tmp = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.tmp = self.tmp.absolute()
        self.addCleanup(self._stack.close)

    def recipe(self, **fields: object) -> os_builder.Recipe:
        values: dict[str, object] = {
            "name": "Test OS",
            "output_dir": str(self.tmp / "out"),
            # Small so that tests which render a wallpaper stay quick; the exact size is
            # covered by test_the_wallpaper_size_setting_is_respected.
            "wallpaper_size": "160x90",
        }
        values.update(fields)
        return os_builder.Recipe(**values)  # type: ignore[arg-type]

    def model(self, **fields: object) -> os_builder.RecipeModel:
        return os_builder.RecipeModel(self.recipe(**fields), base_dir=self.tmp)


# --------------------------------------------------------------------------- #
# the recipe itself
# --------------------------------------------------------------------------- #
class RecipeTests(unittest.TestCase):
    def test_defaults_are_a_sane_desktop(self) -> None:
        recipe = os_builder.Recipe()
        self.assertEqual(recipe.release, "trixie")
        self.assertEqual(recipe.architecture, "amd64")
        self.assertEqual(recipe.compression, "zstd")
        self.assertTrue(recipe.with_wallpaper)
        self.assertTrue(recipe.ready(), msg=str(recipe.problems()))

    def test_every_preset_builds_a_valid_recipe(self) -> None:
        for key in os_builder.PRESETS:
            with self.subTest(preset=key):
                recipe = os_builder.Recipe.for_preset(key)
                self.assertTrue(recipe.ready(), msg=str(recipe.problems()))
                self.assertTrue(recipe.merged_packages())
                self.assertTrue(recipe.accent.startswith("#"))

    def test_as_dict_round_trips_through_json(self) -> None:
        recipe = os_builder.Recipe.for_preset("workstation")
        recipe.packages = ["htop"]
        restored = os_builder.Recipe.from_json(recipe.to_json())
        self.assertEqual(restored.as_dict(), recipe.as_dict())
        self.assertEqual(restored.merged_packages(), recipe.merged_packages())

    def test_the_json_carries_a_schema_version(self) -> None:
        self.assertEqual(os_builder.Recipe().as_dict()["recipe_version"], os_builder.RECIPE_VERSION)

    def test_unknown_keys_are_reported_rather_than_ignored(self) -> None:
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.Recipe.from_dict({"name": "x", "colour_scheme": "blue"})
        self.assertIn("unknown recipe key(s): colour_scheme", str(caught.exception))

    def test_a_newer_recipe_version_refuses_to_load(self) -> None:
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.Recipe.from_dict({"recipe_version": os_builder.RECIPE_VERSION + 7})
        self.assertIn("newer than this builder", str(caught.exception))

    def test_wrong_types_are_named(self) -> None:
        for payload, fragment in (
            ({"packages": "htop"}, "'packages' must be a list of strings"),
            ({"hooks": [1]}, "'hooks' must be a list of strings"),
            ({"name": 3}, "'name' must be a string"),
            ({"boot_test": "yes"}, "'boot_test' must be true or false"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(os_builder.RecipeError) as caught:
                    os_builder.Recipe.from_dict(payload)
                self.assertIn(fragment, str(caught.exception))

    def test_non_object_and_broken_json(self) -> None:
        with self.assertRaises(os_builder.RecipeError):
            os_builder.Recipe.from_dict(["not", "a", "mapping"])
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.Recipe.from_json("{not json")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_missing_files_say_so(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.Recipe.from_file(Path(raw) / "nope.json")
        self.assertIn("cannot read", str(caught.exception))

    def test_for_preset_rejects_unknown_names_and_lists_them(self) -> None:
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.Recipe.for_preset("hacker")
        message = str(caught.exception)
        self.assertIn("no preset called 'hacker'", message)
        self.assertIn("desktop", message)

    def test_save_creates_the_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "nested" / "deeper" / "r.json"
            os_builder.Recipe(name="Saved OS").save(destination)
            self.assertTrue(destination.is_file())
            self.assertEqual(os_builder.Recipe.from_file(destination).name, "Saved OS")

    def test_a_shipped_example_is_valid(self) -> None:
        paths = sorted(Path(os_builder.HERE, "examples").glob("*.json"))
        self.assertGreaterEqual(len(paths), 3, "the shipped recipes are documentation; keep them working")
        for path in paths:
            with self.subTest(example=path.name):
                recipe = os_builder.Recipe.from_file(path)
                self.assertTrue(recipe.ready(), msg=str(recipe.problems()))

    def test_every_field_survives_the_round_trip(self) -> None:
        original = os_builder.Recipe(name="Odd", accent="#123456", packages=["a", "b"], notes="hello")
        clone = os_builder.Recipe.from_dict(original.as_dict())
        for name in original.__dataclass_fields__:
            self.assertEqual(getattr(clone, name), getattr(original, name), msg=name)

    def test_loading_a_recipe_anchors_relative_paths_to_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "recipes"
            folder.mkdir()
            recipe = os_builder.Recipe(name="Anchored", files=["extra.txt:/root/extra.txt"])
            recipe.save(folder / "r.json")
            (folder / "extra.txt").write_text("hi\n", encoding="utf-8")
            loaded = os_builder.Recipe.from_file(folder / "r.json")
            self.assertEqual(loaded.base_dir.name, "recipes")
            self.assertEqual(loaded.problems(), [])


class ValidationTests(TempDirCase):
    def test_problems_name_every_mistake(self) -> None:
        recipe = os_builder.Recipe(
            name="",
            release="potato",
            architecture="riscv",
            compression="bzip2",
            container="sometimes",
            preset="nope",
            wallpaper_style="mosaic",
            wallpaper_size="huge",
            accent="cerulean",
            hostname="not legal!",
        )
        joined = "\n".join(recipe.problems())
        for fragment in (
            "give the OS a name",
            "release 'potato'",
            "architecture 'riscv'",
            "compression 'bzip2'",
            "container mode 'sometimes'",
            "preset 'nope'",
            "wallpaper style 'mosaic'",
            "wallpaper size 'huge'",
            "accent colour 'cerulean'",
            "hostname 'not legal!'",
        ):
            self.assertIn(fragment, joined)
        self.assertFalse(recipe.ready())

    def test_a_one_letter_name_is_a_fine_hostname(self) -> None:
        """Regression: the pattern used to demand at least two characters."""
        recipe = os_builder.Recipe(name="x")
        self.assertEqual(recipe.resolved_hostname, "x")
        self.assertTrue(recipe.ready(), msg=str(recipe.problems()))

    def test_overlong_names_are_rejected(self) -> None:
        self.assertIn("longer than 60 characters", "\n".join(os_builder.Recipe(name="z" * 61).problems()))
        self.assertIn("one line", "\n".join(os_builder.Recipe(name="a\nb").problems()))

    def test_package_names_must_be_plain(self) -> None:
        recipe = os_builder.Recipe(name="Ok", packages=["sudo; rm -rf /", "two words"])
        problems = recipe.problems()
        self.assertEqual(len(problems), 2, msg=str(problems))
        self.assertIn("not a valid Debian package name", problems[0])

    def test_architecture_suffixes_are_allowed(self) -> None:
        recipe = os_builder.Recipe(name="Ok", packages=["vim-amd64", "python3.12", "libstdc++6"])
        self.assertEqual(recipe.problems(), [])

    def test_file_entries_need_a_source_and_a_destination(self) -> None:
        recipe = self.recipe(files=["just-a-path", "broken:"])
        joined = "\n".join(recipe.problems())
        self.assertIn("must be SOURCE:DEST-IN-IMAGE", joined)

    def test_missing_file_sources_are_reported(self) -> None:
        recipe = self.recipe(files=[f"{self.tmp / 'gone.txt'}:/root/gone.txt"])
        self.assertIn("does not exist", "\n".join(recipe.problems()))

    def test_relative_sources_resolve_against_the_recipe_folder(self) -> None:
        (self.tmp / "keys.pub").write_text("ssh-ed25519 AAAA\n", encoding="utf-8")
        recipe = self.recipe(files=["keys.pub:/root/.ssh/authorized_keys"])
        recipe.base_dir = self.tmp
        self.assertEqual(recipe.problems(), [])
        argv = os_builder.recipe_to_argv(recipe)
        copied = argv[argv.index("--copy") + 1]
        self.assertEqual(copied, f"{(self.tmp / 'keys.pub').as_posix()}:/root/.ssh/authorized_keys")

    def test_hooks_must_exist(self) -> None:
        recipe = self.recipe(hooks=[str(self.tmp / "nope.sh")])
        self.assertIn("is not a readable file", "\n".join(recipe.problems()))

    def test_a_valid_recipe_has_no_complaints(self) -> None:
        recipe = self.recipe(name="Fine OS", hostname="fine-os", accent="#00ff88", wallpaper_size="800x600")
        self.assertEqual(recipe.problems(), [])


class HelperTests(unittest.TestCase):
    def test_hostname_for_mirrors_the_builders_sanitiser(self) -> None:
        cases = {
            "Fantastic OS": "fantastic-os",
            "  Spaces  Everywhere  ": "spaces-everywhere",
            "___weird!!!": "weird",
            "café": "caf",
            "": "osmaker",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(os_builder.hostname_for(name), expected)

    def test_a_very_long_name_is_cut_to_a_legal_hostname(self) -> None:
        self.assertEqual(len(os_builder.hostname_for("ab" * 80)), 63)

    def test_default_motd_mentions_the_name_and_tagline(self) -> None:
        text = os_builder.default_motd("Aurora", "bright and quick")
        self.assertIn("Welcome to Aurora.", text)
        self.assertIn("bright and quick", text)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("Welcome to this system.", os_builder.default_motd("", ""))

    def test_parse_package_list_accepts_many_separators(self) -> None:
        self.assertEqual(os_builder.parse_package_list("a,b c,,\td"), ["a", "b", "c", "d"])
        self.assertEqual(os_builder.parse_package_list(["a b", "b,c"]), ["a", "b", "c"])
        self.assertEqual(os_builder.parse_package_list("   "), [])

    def test_parse_size(self) -> None:
        self.assertEqual(os_builder.parse_size("1920x1080"), (1920, 1080))
        self.assertEqual(os_builder.parse_size(" 640 * 480 "), (640, 480))
        for bad in ("big", "1920", "10x10", "99999x99999", "", "1920x1080x768"):
            with self.subTest(bad=bad):
                self.assertIsNone(os_builder.parse_size(bad))

    def test_hex_to_rgb_handles_shorthand_and_junk(self) -> None:
        self.assertEqual(os_builder.hex_to_rgb("#abc"), (170, 187, 204))
        self.assertEqual(os_builder.hex_to_rgb("#4F9cF9"), (79, 156, 249))
        for bad in ("red", "#12345", "#gggggg"):
            with self.subTest(bad=bad), self.assertRaises(os_builder.RecipeError):
                os_builder.hex_to_rgb(bad)

    def test_mix_and_clamp(self) -> None:
        self.assertEqual(os_builder.mix((0, 0, 0), (100, 200, 250), 0.5), (50, 100, 125))
        self.assertEqual(os_builder.mix((10, 10, 10), (20, 20, 20), -5), (10, 10, 10))
        self.assertEqual(os_builder.mix((10, 10, 10), (20, 20, 20), 9), (20, 20, 20))
        self.assertEqual((os_builder.clamp8(-4), os_builder.clamp8(300.6)), (0, 255))

    def test_flag_and_pair_helpers(self) -> None:
        self.assertEqual(os_builder.flag("--x", True), ["--x"])
        self.assertEqual(os_builder.flag("--x", False), [])
        self.assertEqual(os_builder.pairs("--d", "  "), [])
        self.assertEqual(os_builder.pairs("--d", "a/b"), ["--d", str(Path("a/b"))])
        self.assertEqual(os_builder.flat_pairs("--c", ["a", "b"]), ["--c", "a", "--c", "b"])

    def test_describe_argv_quotes_like_a_shell(self) -> None:
        text = os_builder.describe_argv([sys.executable, "-c", "print('hi there')"])
        self.assertIn("print(", text)
        self.assertIn("hi there", text)

    def test_absolutise_only_touches_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).absolute()
            (base / "note.txt").write_text("hi", encoding="utf-8")
            self.assertEqual(
                os_builder.absolutise("note.txt:/etc/note.txt", base),
                f"{(base / 'note.txt').as_posix()}:/etc/note.txt",
            )
            already = os_builder.absolutise(f"{base / 'note.txt'}:/etc/x", base)
            self.assertTrue(already.startswith(f"{base / 'note.txt'}:"))
            self.assertEqual(
                os_builder.absolutise("sub/../note.txt:/x", base), f"{(base / 'note.txt').as_posix()}:/x"
            )
            # an entry without SOURCE:DEST is malformed: validation reports it, and
            # absolutise leaves it alone rather than inventing a destination
            self.assertEqual(os_builder.absolutise("noseparator", base), "noseparator")
            self.assertEqual(os_builder.absolutise("", base), "")

    def test_paths_are_normalised_but_not_resolved(self) -> None:
        """resolve() would follow symlinks; /var is a link on macOS runners."""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).absolute()
            got = os_builder.absolutise_path("a/../b.txt", base)
            self.assertEqual(got, base / "b.txt")

    def test_engine_hint_is_a_human_sentence(self) -> None:
        hint = os_builder.engine_hint()
        self.assertIsInstance(hint, str)
        self.assertGreater(len(hint), 10)

    def test_presets_only_reference_supported_settings(self) -> None:
        for key, preset in os_builder.PRESETS.items():
            with self.subTest(preset=key):
                self.assertTrue(preset.packages, "a preset with no packages is only a name")
                for name in preset.packages:
                    self.assertRegex(name, r"^[a-z0-9+._-]+$")
                self.assertIn(preset.wallpaper, os_builder.WALLPAPER_STYLES)
                self.assertIn(preset.accent, os_builder.ACCENTS)
                self.assertTrue(preset.blurb.endswith("."))

    def test_the_release_list_matches_the_builder(self) -> None:
        import build_os

        self.assertEqual(set(os_builder.RELEASES), set(build_os.KNOWN_RELEASES))
        self.assertEqual(set(os_builder.ARCHITECTURES), set(build_os.ARCHITECTURES))
        self.assertEqual(set(os_builder.COMPRESSIONS), set(build_os.COMPRESSIONS))
        self.assertEqual(os_builder.CONTAINERS, ("auto", "always", "never"))


class ArgvTests(TempDirCase):
    def test_every_recipe_field_reaches_the_builder(self) -> None:
        hook = self.tmp / "h.sh"
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        recipe = self.recipe(
            name="Full",
            preset="custom",
            release="bookworm",
            architecture="arm64",
            compression="lz4",
            container="always",
            packages=["htop", "vim"],
            with_firmware=True,
            boot_test=True,
            keep_work=True,
            skip_checks=True,
            work_dir=str(self.tmp / "work"),
            cache_dir=str(self.tmp / "cache"),
            hooks=[str(hook)],
        )
        argv = os_builder.recipe_to_argv(recipe)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("build_os.py"))
        for option, value in (
            ("--name", "Full"),
            ("--release", "bookworm"),
            ("--architecture", "arm64"),
            ("--compression", "lz4"),
            ("--container", "always"),
            ("--packages", "htop,vim"),
            ("--output", str(self.tmp / "out")),
            ("--work-dir", str(self.tmp / "work")),
            ("--cache-dir", str(self.tmp / "cache")),
            ("--hook", str(hook)),
        ):
            self.assertIn(option, argv)
            self.assertEqual(argv[argv.index(option) + 1], value)
        for switch in ("--with-firmware", "--boot-test", "--keep-work", "--skip-checks"):
            self.assertIn(switch, argv)
        self.assertNotIn("--dry-run", argv)

    def test_omitted_options_are_left_out(self) -> None:
        argv = os_builder.recipe_to_argv(self.recipe(packages=[], preset="custom"))
        for option in (
            "--with-firmware",
            "--boot-test",
            "--keep-work",
            "--skip-checks",
            "--work-dir",
            "--cache-dir",
            "--packages",
            "--copy",
            "--hook",
        ):
            self.assertNotIn(option, argv)

    def test_empty_output_dir_falls_back_to_the_repo_dist(self) -> None:
        argv = os_builder.recipe_to_argv(os_builder.Recipe(output_dir=""))
        self.assertEqual(argv[argv.index("--output") + 1], str(Path(os_builder.HERE) / "dist"))

    def test_branding_travels_as_copy_entries(self) -> None:
        first, second = self.tmp / "a.png", self.tmp / "motd"
        argv = os_builder.recipe_to_argv(self.recipe(), branding=[(first, "/w/a.png"), (second, "/etc/motd")])
        copies = [argv[i + 1] for i, item in enumerate(argv) if item == "--copy"]
        self.assertEqual(len(copies), 2)
        self.assertIn(f"{first.as_posix()}:/w/a.png", copies)

    def test_dry_run_flag_is_opt_in(self) -> None:
        argv = os_builder.recipe_to_argv(self.recipe(preset="custom"), dry_run=True)
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[-1], "--dry-run")

    def test_the_recipe_is_never_mutated(self) -> None:
        recipe = self.recipe(packages=["b", "a"])
        before = recipe.as_dict()
        os_builder.recipe_to_argv(recipe)
        self.assertEqual(recipe.as_dict(), before)

    def test_a_saved_recipe_produces_the_same_command_twice(self) -> None:
        path = self.tmp / "r.json"
        recipe = self.recipe(name="Stable", packages=["htop"])
        recipe.save(path)
        first = os_builder.recipe_to_argv(os_builder.Recipe.from_file(path))
        second = os_builder.recipe_to_argv(os_builder.Recipe.from_file(path))
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# generated artwork
# --------------------------------------------------------------------------- #
class FontTests(unittest.TestCase):
    def test_every_glyph_is_five_by_seven(self) -> None:
        for character, glyph in os_builder.FONT.items():
            with self.subTest(character=character):
                self.assertEqual(len(glyph), os_builder.GLYPH_HEIGHT)
                self.assertTrue(all(len(row) == os_builder.GLYPH_WIDTH for row in glyph))

    def test_the_alphabet_and_digits_are_all_present(self) -> None:
        for character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:-!?":
            self.assertIn(character, os_builder.FONT)

    def test_text_mask_paints_only_known_characters(self) -> None:
        rows = os_builder.text_mask("A")
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0], ".###.")
        self.assertNotIn("#", os_builder.text_mask(" ")[0])

    def test_fit_scale_keeps_long_text_inside_the_canvas(self) -> None:
        short = os_builder.fit_scale("AURORA", 1920, cap=12)
        long = os_builder.fit_scale("A" * 60, 1920, cap=12)
        self.assertGreater(short, long)
        self.assertGreaterEqual(long, 1)
        width = 60 * (os_builder.GLYPH_WIDTH + os_builder.GLYPH_TRACKING) * long
        self.assertLessEqual(width, int(1920 * 0.9))

    def test_ramp_is_a_soft_edge(self) -> None:
        self.assertEqual(os_builder._ramp(-1, 10), 0.0)
        self.assertEqual(os_builder._ramp(11, 10), 0.0)
        self.assertEqual(os_builder._ramp(0, 10), 0.0, "the very edge must fade in, not appear")
        self.assertEqual(os_builder._ramp(5, 10), 1.0)
        self.assertLess(os_builder._ramp(1, 30), os_builder._ramp(15, 30))

    def test_the_scrim_darkens_behind_the_text_only(self) -> None:
        """A pale style must not swallow the product name."""
        bright = (240, 230, 200)
        plain = [[bright for _ in range(120)] for _ in range(40)]
        scrimmed = [list(row) for row in plain]
        os_builder.paint_text(scrimmed, "AURORA", scale=2, colour=(255, 255, 255), anchor_y=0.5, scrim=0.5)
        os_builder.paint_text(plain, "AURORA", scale=2, colour=(255, 255, 255), anchor_y=0.5)
        centre = scrimmed[20]
        self.assertTrue(any(pixel < bright for pixel in centre), "the band behind the glyphs must darken")
        self.assertEqual(scrimmed[0], plain[0], "far from the text nothing may change")
        # and the darkened band must sit behind the glyphs, not replace them
        self.assertNotEqual(scrimmed, plain)

    def test_paint_text_reaches_the_pixels(self) -> None:
        grid = [[(10, 10, 10) for _ in range(80)] for _ in range(30)]
        plain = [row[:] for row in grid]
        os_builder.paint_text(grid, "HI", scale=2, colour=(255, 255, 255), anchor_y=0.5)
        self.assertNotEqual(grid, plain)
        untouched = [row[:] for row in grid]
        os_builder.paint_text(grid, "   ", scale=2, colour=(255, 255, 255), anchor_y=0.5)
        os_builder.paint_text(grid, "HI", scale=0, colour=(255, 255, 255), anchor_y=0.5)
        os_builder.paint_text([], "HI", scale=2, colour=(255, 255, 255), anchor_y=0.5)
        self.assertEqual(grid, untouched, "blank text and a zero scale must not draw")


class WallpaperTests(TempDirCase):
    def test_all_styles_render_at_the_requested_size(self) -> None:
        for style in os_builder.WALLPAPER_STYLES:
            with self.subTest(style=style):
                rows = os_builder.wallpaper_pixels(self.recipe(wallpaper_style=style), 120, 80)
                self.assertEqual(len(rows), 80)
                self.assertTrue(all(len(row) == 120 for row in rows))

    def test_the_accent_colour_actually_changes_the_image(self) -> None:
        first = os_builder.wallpaper_pixels(self.recipe(accent="#ff0000", with_wallpaper=False), 60, 40)
        second = os_builder.wallpaper_pixels(self.recipe(accent="#0000ff", with_wallpaper=False), 60, 40)
        self.assertNotEqual(first, second)

    def test_pixels_stay_in_range_and_are_not_flat(self) -> None:
        rows = os_builder.wallpaper_pixels(self.recipe(wallpaper_style="waves"), 90, 60)
        flat = [channel for row in rows for pixel in row for channel in pixel]
        self.assertTrue(all(0 <= value <= 255 for value in flat))
        self.assertGreater(len(set(flat)), 50, "a flat image means the style is not drawing")

    def test_the_name_is_stamped_unless_asked_not_to_be(self) -> None:
        with_text = os_builder.wallpaper_pixels(self.recipe(name="Zephyr", with_wallpaper=True), 160, 90)
        without = os_builder.wallpaper_pixels(self.recipe(name="Zephyr", with_wallpaper=False), 160, 90)
        self.assertNotEqual(with_text, without)

    def test_rendering_is_deterministic(self) -> None:
        recipe = self.recipe(name="Same", wallpaper_style="rays", accent="#123abc")
        self.assertEqual(
            os_builder.wallpaper_pixels(recipe, 64, 40), os_builder.wallpaper_pixels(recipe, 64, 40)
        )

    def test_write_png_writes_a_readable_rgb_png(self) -> None:
        rows = [[(1, 2, 3), (4, 5, 6)], [(7, 8, 9), (10, 11, 12)]]
        path = self.tmp / "out.png"
        self.assertEqual(os_builder.write_png(path, rows), (2, 2))
        blob = path.read_bytes()
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(os_builder.read_png_size(path), (2, 2))
        raw = zlib.decompress(blob[blob.index(b"IDAT") + 4 : blob.index(b"IEND") - 4])
        self.assertEqual(raw, bytes([0, 1, 2, 3, 4, 5, 6, 0, 7, 8, 9, 10, 11, 12]))
        self.assertIn(b"IHDR", blob)

    def test_a_real_wallpaper_decodes_and_matches_the_header(self) -> None:
        path = self.tmp / "w.png"
        os_builder.render_wallpaper_png(
            self.recipe(name="Wide", wallpaper_size="200x100"), path, width=200, height=100
        )
        self.assertEqual(os_builder.read_png_size(path), (200, 100))
        blob = path.read_bytes()
        raw = zlib.decompress(blob[blob.index(b"IDAT") + 4 : blob.index(b"IEND") - 4])
        self.assertEqual(len(raw), 100 * (200 * 3 + 1), "one filter byte plus three channels per pixel")
        self.assertEqual(set(raw[::601]), {0}, "every scanline must start with filter type 0")

    def test_write_png_rejects_empty_input_and_reports_failures(self) -> None:
        with self.assertRaises(os_builder.RecipeError):
            os_builder.write_png(self.tmp / "none.png", [])
        blocked = self.tmp / "locked"
        blocked.write_text("I am a file, not a directory", encoding="utf-8")
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.write_png(blocked / "x.png", [[(0, 0, 0)]])
        self.assertIn("cannot write", str(caught.exception))

    def test_read_png_size_checks_the_header(self) -> None:
        fake = self.tmp / "fake.png"
        fake.write_bytes(b"not a png at all, really")
        with self.assertRaises(os_builder.RecipeError) as caught:
            os_builder.read_png_size(fake)
        self.assertIn("is not a PNG file", str(caught.exception))
        with self.assertRaises(os_builder.RecipeError):
            os_builder.read_png_size(self.tmp / "absent.png")

    def test_save_preview_png_makes_a_file_of_the_right_size(self) -> None:
        path = os_builder.save_preview_png(self.recipe(), self.tmp / "p.png", width=96, height=54)
        self.assertEqual(os_builder.read_png_size(path), (96, 54))


class BrandingTests(TempDirCase):
    def test_make_branding_writes_every_artefact(self) -> None:
        branding = os_builder.make_branding(self.recipe(wallpaper_size="1920x1080"), self.tmp / "b")
        self.assertEqual(
            sorted(path.name for path in (self.tmp / "b").iterdir()),
            ["branding.hook.chroot", "issue", "motd", "os-release", "wallpaper.png"],
        )
        self.assertEqual(len(branding.copies), 4)
        self.assertEqual(branding.size, (1920, 1080))
        self.assertGreater(branding.bytes, 1000)
        for source, destination in branding.copies:
            self.assertTrue(source.is_file(), str(source))
            self.assertTrue(destination.startswith("/"), destination)

    def test_the_wallpaper_can_be_left_out_of_the_image(self) -> None:
        branding = os_builder.make_branding(self.recipe(with_wallpaper=False), self.tmp / "b")
        self.assertIsNone(branding.wallpaper)
        self.assertFalse(any("backgrounds" in destination for _source, destination in branding.copies))
        self.assertNotIn("wallpaper.png", branding.hook.read_text(encoding="utf-8"))
        self.assertEqual(len(branding.copies), 3)

    def test_the_wallpaper_size_setting_is_respected(self) -> None:
        branding = os_builder.make_branding(self.recipe(wallpaper_size="160x90"), self.tmp / "b")
        assert branding.wallpaper is not None
        self.assertEqual(os_builder.read_png_size(branding.wallpaper), (160, 90))

    def test_generated_text_files_say_the_right_things(self) -> None:
        recipe = self.recipe(
            name="Aurora", tagline="bright", accent="#010203", preset="desktop", motd="hello\n"
        )
        directory = self.tmp / "b"
        os_builder.make_branding(recipe, directory)
        self.assertEqual((directory / "motd").read_text(encoding="utf-8"), "hello\n")
        issue = (directory / "issue").read_text(encoding="utf-8")
        self.assertIn("Aurora", issue)
        self.assertIn("bright", issue)
        self.assertIn("trixie", issue)
        release = (directory / "os-release").read_text(encoding="utf-8")
        self.assertIn('PRETTY_NAME="Aurora"', release)
        self.assertIn("ID=aurora", release, "identifiers stay bare, as Debian writes them")
        self.assertIn("ID_LIKE=debian", release)
        self.assertIn('OS_MAKER_ACCENT="#010203"', release)
        self.assertIn('HOME_URL="https://github.com/codero-sus/OS-Maker"', release)

    def test_the_default_motd_is_used_when_none_is_given(self) -> None:
        directory = self.tmp / "b"
        os_builder.make_branding(self.recipe(name="Motd OS", motd=""), directory)
        self.assertIn("Welcome to Motd OS.", (directory / "motd").read_text(encoding="utf-8"))

    def test_os_release_quotes_only_when_it_must(self) -> None:
        text = os_builder.os_release_text(self.recipe(name="Plain", tagline=""))
        pairs = dict(line.split("=", 1) for line in text.strip().splitlines() if "=" in line)
        self.assertEqual(pairs["VERSION_ID"], '"1.0"', "Debian quotes version ids")
        self.assertEqual(pairs["ID_LIKE"], "debian", "identifiers are the exception")
        self.assertEqual(pairs["ID"], "plain")
        self.assertTrue(pairs["PRETTY_NAME"].startswith('"'))
        self.assertTrue(pairs["HOME_URL"].startswith('"'))

    def test_the_hook_is_valid_shell(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("no POSIX shell on this machine")
        branding = os_builder.make_branding(self.recipe(), self.tmp / "b")
        completed = subprocess.run(
            [shell, "-n", str(branding.hook)], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        text = branding.hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"), text[:20])
        self.assertIn("set -eu", text)
        for destination in ("/etc/motd", "/etc/issue", "/etc/os-release"):
            self.assertIn(destination, text)
        self.assertIn("os-builder: branding installed", text)

    def test_make_branding_creates_missing_directories(self) -> None:
        deep = self.tmp / "a" / "b" / "c"
        os_builder.make_branding(self.recipe(), deep)
        self.assertTrue((deep / "motd").is_file())

    def test_hook_file_name_matches_what_live_build_runs(self) -> None:
        branding = os_builder.make_branding(self.recipe(), self.tmp / "b")
        self.assertTrue(branding.hook.name.endswith(".hook.chroot"), branding.hook.name)


# --------------------------------------------------------------------------- #
# progress, artefacts and the runner
# --------------------------------------------------------------------------- #
def walked(lines: tuple[str, ...]) -> list[float]:
    progress = os_builder.Progress()
    seen = []
    for line in lines:
        progress = os_builder.progress_from_line(line, progress)
        seen.append(progress.fraction)
    return seen


class ProgressTests(unittest.TestCase):
    def test_strip_ansi_removes_colours_and_carriage_returns(self) -> None:
        self.assertEqual(os_builder.strip_ansi("\x1b[1;31mhot\x1b[0m"), "hot")
        self.assertEqual(os_builder.strip_ansi("a\r\nb"), "a\nb")
        self.assertEqual(os_builder.strip_ansi("no escapes"), "no escapes")

    def test_stage_markers_move_the_estimate_forward(self) -> None:
        transcript = (
            "installing live-build ...",
            "I: Configuration",
            "I: Bootstrap stage",
            "Get:1 http://deb.debian.org [42%]",
            "I: Binary ISO",
            "Bootable ISO created!",
        )
        seen = walked(transcript)
        self.assertEqual(seen, sorted(seen), "the bar must never go backwards")
        self.assertEqual(seen[0], 0.18)
        self.assertAlmostEqual(seen[-1], 0.97, places=2)

    def test_blank_and_unknown_lines_keep_the_estimate(self) -> None:
        start = os_builder.Progress(label="custom", fraction=0.4, lines=3)
        self.assertEqual(os_builder.progress_from_line("   ", start), start)
        kept = os_builder.progress_from_line("some chatter", start)
        self.assertEqual(kept.fraction, 0.4)
        self.assertEqual(kept.lines, start.lines + 1)
        self.assertEqual(kept.label, "custom")

    def test_progress_never_claims_done_early(self) -> None:
        progress = os_builder.Progress()
        for line in ("I: Binary ISO", "hybrid MBR installed"):
            progress = os_builder.progress_from_line(line, progress)
        self.assertLessEqual(progress.fraction, 0.97)
        self.assertFalse(progress.done)

    def test_apt_percentage_is_only_used_early_on(self) -> None:
        early = os_builder.Progress(label="downloading packages", fraction=0.15, apt_percent=33.0)
        self.assertEqual(early.text, "downloading packages - 33%")
        late = os_builder.Progress(label="writing the ISO", fraction=0.9, apt_percent=33.0)
        self.assertEqual(late.text, "writing the ISO")
        self.assertEqual(os_builder.Progress(label="build finished", done=True).text, "build finished")

    def test_get_lines_start_the_download_stage(self) -> None:
        progress = os_builder.progress_from_line("Get:1 http://x [10%]", os_builder.Progress())
        self.assertEqual(progress.label, "downloading packages")
        self.assertEqual(progress.apt_percent, 10.0)

    def test_error_and_warning_patterns_match_builder_output(self) -> None:
        self.assertIsNotNone(os_builder.ERROR_RE.search("os-maker: no engine found"))
        self.assertIsNotNone(os_builder.ERROR_RE.search("E: Unable to locate package nope"))
        self.assertIsNotNone(os_builder.WARNING_RE.search("os-maker: warning: no kernel in --packages"))
        self.assertIsNone(os_builder.WARNING_RE.search("nothing to see here"))


class ArtifactTests(TempDirCase):
    def test_only_build_artefacts_are_listed(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        (out / "aurora.iso").write_bytes(b"x" * 10)
        (out / "aurora.iso.sha256").write_text("abc  aurora.iso\n", encoding="utf-8")
        (out / "aurora.log").write_text("log\n", encoding="utf-8")
        (out / "notes.txt").write_text("ignore me\n", encoding="utf-8")
        (out / "subdir.iso").mkdir()
        found = os_builder.list_artifacts(out)
        self.assertEqual(
            sorted(item.path.name for item in found), ["aurora.iso", "aurora.iso.sha256", "aurora.log"]
        )
        self.assertEqual({item.kind for item in found}, {"disk image", "checksum", "build log"})

    def test_a_missing_directory_is_simply_empty(self) -> None:
        self.assertEqual(os_builder.list_artifacts(self.tmp / "nope"), [])

    def test_since_filters_out_older_files(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        (out / "old.iso").write_bytes(b"1")
        stamp = os.path.getmtime(out) - 600
        os.utime(out / "old.iso", (stamp, stamp))
        (out / "new.iso").write_bytes(b"2")
        recent = os_builder.list_artifacts(out, since=os.path.getmtime(out / "new.iso") - 1)
        self.assertEqual([item.path.name for item in recent], ["new.iso"])

    def test_results_are_newest_first(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        for index, name in enumerate(("a.iso", "b.iso", "c.iso")):
            (out / name).write_bytes(b"x")
            os.utime(out / name, (1_700_000_000 + index * 60, 1_700_000_000 + index * 60))
        self.assertEqual(
            [item.path.name for item in os_builder.list_artifacts(out)], ["c.iso", "b.iso", "a.iso"]
        )

    def test_human_size(self) -> None:
        for size, expected in (
            (0, "0 B"),
            (512, "512 B"),
            (2048, "2.0 KiB"),
            (5 * 1024**2, "5.0 MiB"),
            (3 * 1024**3, "3.0 GiB"),
            (40 * 1024**3, "40.0 GiB"),
        ):
            with self.subTest(size=size):
                self.assertEqual(os_builder.Artifact(self.tmp, "disk image", size, 0.0).human_size, expected)

    def test_age_is_relative_and_readable(self) -> None:
        now = 1_700_000_000.0
        with unittest.mock.patch.object(os_builder.time, "time", return_value=now + 5):
            self.assertEqual(os_builder.Artifact(self.tmp, "disk image", 1, now).age, "just now")
        with unittest.mock.patch.object(os_builder.time, "time", return_value=now + 600):
            self.assertEqual(os_builder.Artifact(self.tmp, "disk image", 1, now).age, "10 min ago")
        with unittest.mock.patch.object(os_builder.time, "time", return_value=now + 86_400):
            self.assertRegex(os_builder.Artifact(self.tmp, "disk image", 1, now).age, r"^\d\d:\d\d$")

    def test_verify_checksum_accepts_a_good_sidecar(self) -> None:
        import hashlib

        iso = self.tmp / "os.iso"
        iso.write_bytes(b"fantastic bytes")
        digest = hashlib.sha256(iso.read_bytes()).hexdigest()
        iso.with_suffix(".iso.sha256").write_text(f"{digest}  os.iso\n", encoding="utf-8")
        ok, message = os_builder.verify_checksum(iso)
        self.assertTrue(ok)
        self.assertIn("matches", message)
        self.assertIn(digest[:16], message)

    def test_verify_checksum_reports_mismatch_absence_and_garbage(self) -> None:
        iso = self.tmp / "os.iso"
        iso.write_bytes(b"data")
        ok, message = os_builder.verify_checksum(iso)
        self.assertFalse(ok)
        self.assertIn("no checksum file", message)
        sidecar = iso.with_suffix(".iso.sha256")
        sidecar.write_text("0" * 64 + "  os.iso\n", encoding="utf-8")
        ok, message = os_builder.verify_checksum(iso)
        self.assertFalse(ok)
        self.assertIn("mismatch", message)
        sidecar.write_text("md5 = 1234\n", encoding="utf-8")
        ok, message = os_builder.verify_checksum(iso)
        self.assertFalse(ok)
        self.assertIn("no SHA-256 digest", message)

    def test_verify_checksum_survives_an_unreadable_image(self) -> None:
        iso = self.tmp / "gone.iso"
        iso.with_suffix(".iso.sha256").write_text("a" * 64 + "\n", encoding="utf-8")
        ok, message = os_builder.verify_checksum(iso)
        self.assertFalse(ok)
        self.assertIn("cannot read", message)


class LoaderTests(TempDirCase):
    def test_load_recipe_prefers_a_file_then_a_preset_then_defaults(self) -> None:
        path = self.tmp / "r.json"
        self.recipe(name="From File").save(path)
        self.assertEqual(os_builder.load_recipe(path).name, "From File")
        self.assertEqual(os_builder.load_recipe(None, preset="gaming").preset, "gaming")
        self.assertEqual(os_builder.load_recipe(None).name, "Fantastic OS")

    def test_load_recipe_reports_a_bad_preset(self) -> None:
        with self.assertRaises(os_builder.RecipeError):
            os_builder.load_recipe(None, preset="vapourware")

    def test_preview_reports_an_unreadable_file(self) -> None:
        with captured() as box:
            self.assertEqual(os_builder.preview(self.tmp / "missing.json"), 1)
        self.assertIn("cannot read", box.stderr)

    def test_wallpaper_falls_back_when_the_size_is_nonsense(self) -> None:
        path = self.tmp / "s.json"
        os_builder.Recipe(name="Sized", wallpaper_size="giant").save(path)
        out = self.tmp / "s.png"
        with captured():
            self.assertEqual(os_builder.write_wallpaper(path, out), 0)
        self.assertEqual(os_builder.read_png_size(out), (640, 360))


class BuildRunTests(TempDirCase):
    SCRIPT = (
        "import sys\n"
        "print('installing live-build ...', flush=True)\n"
        "print('I: Bootstrap stage', flush=True)\n"
        "print('\\x1b[31mos-maker: warning: careful\\x1b[0m', flush=True)\n"
        "sys.stderr.write('to the log\\n')\n"
        "sys.exit(int(sys.argv[1]))\n"
    )

    def fake_run(self, code: str) -> os_builder.BuildRun:
        script = self.tmp / "fake-builder.py"
        script.write_text(self.SCRIPT, encoding="utf-8")
        run = os_builder.BuildRun([sys.executable, str(script), code], cwd=self.tmp)
        run.start()
        return run

    def wait_for_exit(self, run: os_builder.BuildRun, limit: float = 30.0) -> None:
        deadline = os_builder.time.monotonic() + limit
        while run.running and os_builder.time.monotonic() < deadline:
            os_builder.time.sleep(0.02)
        self.assertFalse(run.running, "the fake builder should have exited")

    def test_a_finished_run_reports_lines_exit_code_and_progress(self) -> None:
        run = self.fake_run("0")
        self.wait_for_exit(run)
        self.assertEqual(run.finish_code, 0)
        self.assertTrue(any("installing live-build" in line for line in run.log))
        self.assertTrue(any("warning: careful" in line for line in run.log), "stderr must be merged in")
        self.assertNotIn("\x1b", "".join(run.log), "escape codes must be stripped for the log")
        self.assertGreater(run.progress.fraction, 0.3)
        self.assertTrue(run.progress.done)
        self.assertTrue(run.progress.ok)
        self.assertEqual(run.progress.label, "build finished")
        self.assertGreaterEqual(run.elapsed, 0.0)

    def test_a_failing_run_is_marked_failed(self) -> None:
        run = self.fake_run("3")
        self.wait_for_exit(run)
        self.assertEqual(run.finish_code, 3)
        self.assertFalse(run.progress.ok)
        self.assertIn("exit 3", run.progress.label)

    def test_drain_hands_back_each_line_once(self) -> None:
        run = self.fake_run("0")
        self.wait_for_exit(run)
        first = run.drain()
        self.assertTrue(first)
        self.assertEqual(run.drain(), [], "lines are consumed exactly once")
        self.assertEqual(run.progress.lines, len(first), "the end marker must not be swallowed or lost")

    def test_stop_terminates_a_long_build(self) -> None:
        script = self.tmp / "sleeper.py"
        script.write_text("import time\nprint('starting', flush=True)\ntime.sleep(300)\n", encoding="utf-8")
        run = os_builder.BuildRun([sys.executable, str(script)], cwd=self.tmp)
        run.start()
        deadline = os_builder.time.monotonic() + 15
        while not run.log and os_builder.time.monotonic() < deadline:
            os_builder.time.sleep(0.02)
        run.stop()
        if run.reader is not None:
            run.reader.join(timeout=30)
        self.assertIsNotNone(run.finish_code)
        self.assertNotEqual(run.finish_code, 0)
        self.assertFalse(run.running)

    def test_starting_twice_or_with_a_missing_script_is_an_error(self) -> None:
        run = os_builder.BuildRun([sys.executable, str(self.tmp / "nowhere.py")])
        with self.assertRaises(os_builder.RecipeError) as caught:
            run.start()
        self.assertIn("cannot find the builder", str(caught.exception))
        script = self.tmp / "ok.py"
        script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        good = os_builder.BuildRun([sys.executable, str(script)])
        good.start()
        try:
            with self.assertRaises(os_builder.RecipeError):
                good.start()
        finally:
            good.stop()

    def test_stop_before_start_is_harmless(self) -> None:
        run = os_builder.BuildRun([sys.executable, "-c", "pass"])
        run.stop()
        self.assertIsNone(run.finish_code)
        run._pump()  # the reader guards against a process that never appeared
        self.assertEqual(run.drain(), [])

    def test_argv_and_cwd_are_remembered_as_strings(self) -> None:
        run = os_builder.BuildRun([sys.executable, self.tmp / "x.py"], cwd=self.tmp)
        self.assertEqual(run.argv, [sys.executable, str(self.tmp / "x.py")])
        self.assertEqual(run.cwd, self.tmp)
        self.assertEqual(run.elapsed, 0.0)

    def test_an_unstartable_program_is_reported_not_raised(self) -> None:
        script = self.tmp / "gone.py"
        script.write_text("print('x')", encoding="utf-8")
        run = os_builder.BuildRun(["/nonexistent/interpreter-of-no-kind", str(script)])
        with (
            unittest.mock.patch.object(os_builder.subprocess, "Popen", side_effect=OSError("nope")),
            self.assertRaises(os_builder.RecipeError) as caught,
        ):
            run.start()
        self.assertIn("cannot start the builder", str(caught.exception))


# --------------------------------------------------------------------------- #
# the model the GUI drives
# --------------------------------------------------------------------------- #
class ModelTests(TempDirCase):
    def test_set_validates_field_names_and_notifies(self) -> None:
        model = self.model()
        seen: list[int] = []
        model.subscribe(lambda: seen.append(1))
        model.set(name="Renamed")
        self.assertEqual(model.recipe.name, "Renamed")
        self.assertEqual(len(seen), 1)
        with self.assertRaises(os_builder.RecipeError):
            model.set(nonsense=1)

    def test_preset_switch_adopts_packages_and_keeps_a_custom_name(self) -> None:
        model = self.model(name="My Own", tagline="mine", preset="minimal")
        model.set_preset("gaming")
        self.assertEqual(model.recipe.preset, "gaming")
        self.assertEqual(model.recipe.name, "My Own", "a name the user typed is theirs")
        self.assertTrue(model.recipe.with_firmware)
        self.assertIn("retroarch", model.recipe.merged_packages())

    def test_preset_switch_replaces_a_preset_name_and_appearance(self) -> None:
        model = self.model(name="Minimal live system", accent="#abcdef", wallpaper_style="sunset")
        model.set_preset("kiosk")
        self.assertEqual(model.recipe.name, "Single-page kiosk")
        self.assertEqual(model.recipe.accent, os_builder.PRESETS["kiosk"].accent)
        self.assertEqual(model.recipe.wallpaper_style, os_builder.PRESETS["kiosk"].wallpaper)

    def test_preset_switch_can_keep_the_look(self) -> None:
        model = self.model(name="Other", accent="#abcdef", wallpaper_style="sunset", preset="minimal")
        model.set_preset("workstation", keep_appearance=True)
        self.assertEqual(model.recipe.accent, "#abcdef")
        self.assertEqual(model.recipe.wallpaper_style, "sunset")
        self.assertEqual(model.recipe.packages, [])

    def test_unknown_preset_is_refused(self) -> None:
        with self.assertRaises(os_builder.RecipeError):
            self.model().set_preset("nope")

    def test_package_toggling_is_a_switch_not_a_magnet(self) -> None:
        model = self.model()
        self.assertTrue(model.toggle_package("vim"))
        self.assertTrue(model.toggle_package("vim", on=True))
        self.assertEqual(model.recipe.packages, ["vim"])
        self.assertFalse(model.toggle_package("vim", on=False))
        self.assertEqual(model.recipe.packages, [])

    def test_add_packages_batches_and_deduplicates(self) -> None:
        model = self.model(packages=["curl"])
        self.assertEqual(model.add_packages("htop, vim htop"), ["htop", "vim"])
        self.assertEqual(model.recipe.packages, ["curl", "htop", "vim"])
        self.assertEqual(model.add_packages("curl"), [])

    def test_file_and_hook_lists_are_editable_by_index(self) -> None:
        (self.tmp / "key.pub").write_text("ssh\n", encoding="utf-8")
        model = self.model()
        entry = model.add_file(self.tmp / "key.pub", "/root/.ssh/authorized_keys")
        self.assertTrue(entry.endswith(":/root/.ssh/authorized_keys"))
        self.assertEqual(model.problems(), [])
        model.add_file(self.tmp / "key.pub", "")
        self.assertEqual(len(model.recipe.files), 2)
        self.assertTrue(model.recipe.files[1].endswith(":/root/"))
        model.remove_file(0)
        model.remove_file(99)
        self.assertEqual(len(model.recipe.files), 1)
        model.add_hook(self.tmp / "key.pub")
        model.remove_hook(0)
        model.remove_hook(-1)
        self.assertEqual(model.recipe.hooks, [])

    def test_adding_the_same_file_twice_is_one_entry(self) -> None:
        model = self.model()
        first = model.add_file("a.txt", "/etc/a")
        model.add_file("a.txt", "/etc/a")
        self.assertEqual(model.recipe.files, [first])

    def test_suggestions_cover_every_preset(self) -> None:
        model = self.model()
        self.assertIn("task-desktop", model.suggestions)
        self.assertIn("retroarch", model.suggestions)
        self.assertEqual(model.suggestions, sorted(set(model.suggestions)))

    def test_argv_includes_generated_branding(self) -> None:
        argv = self.model(packages=["htop"]).argv()
        self.assertIn("--packages", argv)
        destinations = [argv[i + 1].partition(":")[2] for i, item in enumerate(argv) if item == "--copy"]
        self.assertIn("/etc/motd", destinations)
        self.assertIn("/usr/share/backgrounds/os-maker.png", destinations)
        self.assertNotIn("--dry-run", argv)

    def test_preview_image_writes_a_png(self) -> None:
        path = self.model().preview_image(self.tmp, width=64, height=40)
        self.assertEqual(os_builder.read_png_size(path), (64, 40))

    def test_save_and_open_round_trip_through_the_model(self) -> None:
        model = self.model(name="Round Trip", packages=["vim"])
        path = model.save(self.tmp / "sub" / "recipe.json")
        self.assertEqual(model.path, path)
        other = os_builder.RecipeModel(base_dir=self.tmp)
        loaded = other.open(path)
        self.assertEqual(loaded.name, "Round Trip")
        self.assertEqual(other.recipe.packages, ["vim"])
        self.assertEqual(other.path, path)

    def test_a_saved_model_recipe_is_anchored_where_it_was_written(self) -> None:
        folder = self.tmp / "anchor"
        model = self.model(files=["extra:/root/extra"])
        model.save(folder / "r.json")
        self.assertEqual(model.open(folder / "r.json").base_dir, folder)

    def test_branding_directory_is_reused_and_cleaned_up(self) -> None:
        model = self.model()
        directory = model.branding_dir()
        self.assertEqual(model.branding_dir(), directory)
        self.assertTrue(directory.parent.is_dir(), "the scratch root exists until the run ends")
        os_builder.make_branding(model.recipe, directory)
        self.assertTrue((directory / "motd").is_file())
        model.close()
        self.assertFalse(directory.parent.exists())
        self.assertIsNone(model.scratch)
        model.close()

    def test_start_build_refuses_a_broken_recipe(self) -> None:
        model = self.model(name="", release="nope")
        with self.assertRaises(os_builder.RecipeError) as caught:
            model.start_build()
        self.assertIn("give the OS a name", str(caught.exception))

    def test_start_build_refuses_to_stack_on_a_running_build(self) -> None:
        model = self.model()
        model.run = unittest.mock.MagicMock(running=True)
        with self.assertRaises(os_builder.RecipeError) as caught:
            model.start_build()
        self.assertIn("already running", str(caught.exception))

    def test_start_build_launches_the_real_builder(self) -> None:
        model = self.model(name="Live Start")
        run = model.start_build(dry_run=True)
        self.assertIsNotNone(run.process)
        deadline = os_builder.time.monotonic() + 60
        while run.running and os_builder.time.monotonic() < deadline:
            os_builder.time.sleep(0.02)
        run.drain()
        self.assertEqual(run.finish_code, 0, "\n".join(run.log))
        self.assertTrue(run.progress.done)
        self.assertTrue(any("Planned build" in line for line in run.log))

    def test_dry_run_through_the_model_reaches_the_real_builder(self) -> None:
        """End to end: model -> argv -> build_os.py --dry-run, which must approve the plan."""
        code, text = self.model(name="Smoke", packages=["htop"]).preview()
        self.assertEqual(code, 0, text)
        self.assertIn("Planned build", text)
        self.assertIn("htop", text)

    def test_artifacts_come_from_the_output_directory(self) -> None:
        (self.tmp / "out").mkdir()
        (self.tmp / "out" / "smoke.iso").write_bytes(b"x" * 40)
        self.assertEqual([item.path.name for item in self.model().artifacts()], ["smoke.iso"])

    def test_ready_reflects_the_recipe(self) -> None:
        model = self.model()
        self.assertTrue(model.ready())
        model.set(release="potato")
        self.assertFalse(model.ready())


# --------------------------------------------------------------------------- #
# the headless command line
# --------------------------------------------------------------------------- #
class CliTests(TempDirCase):
    def write_recipe(self, **fields: object) -> Path:
        path = self.tmp / "recipe.json"
        self.recipe(**fields).save(path)
        return path

    def test_list_presets_shows_every_one_with_its_packages(self) -> None:
        with captured() as box:
            code = os_builder.main(["--list-presets"])
        self.assertEqual(code, 0)
        for key, preset in os_builder.PRESETS.items():
            self.assertIn(key, box.stdout)
            self.assertIn(preset.label, box.stdout)
            self.assertIn(preset.packages[0], box.stdout)

    def test_version_and_help_exit_cleanly(self) -> None:
        for argv in (["--version"], ["--help"]):
            with self.subTest(argv=argv), captured():
                with self.assertRaises(SystemExit) as caught:
                    os_builder.main(argv)
                self.assertEqual(caught.exception.code, 0)

    def test_an_unknown_option_is_a_usage_error(self) -> None:
        with captured(), self.assertRaises(SystemExit) as caught:
            os_builder.main(["--nope"])
        self.assertEqual(caught.exception.code, 2)

    def test_new_writes_a_recipe_that_passes_check(self) -> None:
        destination = self.tmp / "starter.json"
        with captured() as box:
            self.assertEqual(os_builder.main(["--new", "--out", str(destination), "--preset", "server"]), 0)
        self.assertIn("wrote", box.stdout)
        with captured() as check:
            self.assertEqual(os_builder.main(["--check", str(destination)]), 0)
        self.assertIn("looks good", check.stdout)
        self.assertEqual(os_builder.Recipe.from_file(destination).preset, "server")

    def test_new_without_a_preset_is_the_default_recipe(self) -> None:
        destination = self.tmp / "plain.json"
        with captured():
            os_builder.main(["--new", "--out", str(destination)])
        self.assertEqual(os_builder.Recipe.from_file(destination).name, "Fantastic OS")

    def test_check_reports_every_problem_and_exits_two(self) -> None:
        path = self.tmp / "broken.json"
        path.write_text(json.dumps({"name": "", "release": "nope", "accent": "blue"}), encoding="utf-8")
        with captured() as box:
            self.assertEqual(os_builder.main(["--check", str(path)]), 2)
        self.assertIn("give the OS a name", box.stderr)
        self.assertIn("accent colour", box.stderr)
        self.assertIn("broken.json:", box.stderr, "the offending file should be named")

    def test_check_with_an_unparsable_file(self) -> None:
        path = self.tmp / "junk.json"
        path.write_text("{oops", encoding="utf-8")
        with captured() as box:
            self.assertEqual(os_builder.main(["--check", str(path)]), 1)
        self.assertIn("not valid JSON", box.stderr)

    def test_check_accepts_several_files_and_fails_if_any_is_bad(self) -> None:
        good = self.write_recipe()
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"name": "", "release": "nope"}), encoding="utf-8")
        with captured():
            self.assertEqual(os_builder.main(["--check", str(good)]), 0)
            self.assertEqual(os_builder.main(["--check", str(good), str(bad)]), 2)

    def test_check_of_a_missing_file_is_not_a_crash(self) -> None:
        with captured() as box:
            self.assertEqual(os_builder.main(["--check", str(self.tmp / "gone.json")]), 1)
        self.assertIn("cannot read", box.stderr)

    def test_wallpaper_mode_writes_a_png_at_the_recipe_size(self) -> None:
        path = self.write_recipe(wallpaper_size="320x200", wallpaper_style="waves", name="Art")
        out = self.tmp / "art.png"
        with captured() as box:
            self.assertEqual(os_builder.main(["--wallpaper", str(path), "--out", str(out)]), 0)
        self.assertIn("320x200", box.stdout)
        self.assertEqual(os_builder.read_png_size(out), (320, 200))

    def test_wallpaper_mode_defaults_to_a_named_file(self) -> None:
        path = self.write_recipe(name="Default Name")
        with captured() as box:
            self.assertEqual(os_builder.main(["--wallpaper", str(path)]), 0)
        self.assertIn("default-name-wallpaper.png", box.stdout)
        written = Path(os_builder.HERE) / "default-name-wallpaper.png"
        self.addCleanup(written.unlink, missing_ok=True)
        self.assertTrue(written.is_file())

    def test_preview_prints_the_command_and_the_builders_plan(self) -> None:
        path = self.write_recipe(name="Preview OS", packages=["htop"])
        with captured() as box:
            code = os_builder.main(["--preview", str(path)])
        self.assertEqual(code, 0, box.text)
        self.assertIn("build_os.py", box.stdout)
        self.assertIn("--dry-run", box.stdout)
        self.assertIn("Planned build", box.stdout)
        self.assertIn("branded files", box.stdout)

    def test_preview_passes_through_a_failing_plan(self) -> None:
        path = self.write_recipe(name="Bad Plan", release="nonsense")
        with (
            unittest.mock.patch.object(
                os_builder, "plan_summary", return_value=(2, "os-maker: nope")
            ) as stub,
            captured() as box,
        ):
            self.assertEqual(os_builder.main(["--preview", str(path)]), 2)
        self.assertIn("os-maker: nope", box.stdout)
        self.assertTrue(stub.called)

    def test_preview_warns_about_a_recipe_it_can_still_plan(self) -> None:
        path = self.tmp / "warn.json"
        os_builder.Recipe(name="", release="nope").save(path)
        with captured() as box:
            os_builder.main(["--preview", str(path)])
        self.assertIn("warning", box.stderr)
        self.assertIn("give the OS a name", box.stderr)

    def test_preview_cleans_up_its_temporary_branding(self) -> None:
        path = self.write_recipe()
        made: list[Path] = []
        original = os_builder.make_branding

        def spy(recipe: os_builder.Recipe, directory: Path) -> os_builder.Branding:
            made.append(directory)
            return original(recipe, directory)

        with unittest.mock.patch.object(os_builder, "make_branding", spy), captured():
            os_builder.main(["--preview", str(path)])
        self.assertEqual(len(made), 1)
        self.assertFalse(made[0].exists(), "the scratch directory must not be left behind")

    def test_build_refuses_an_invalid_recipe_before_running_anything(self) -> None:
        path = self.tmp / "bad.json"
        path.write_text(json.dumps({"name": "Ok", "release": "nonsense"}), encoding="utf-8")
        with (
            unittest.mock.patch.object(os_builder, "BuildRun", side_effect=AssertionError("must not run")),
            captured() as box,
        ):
            self.assertEqual(os_builder.main(["--build", str(path)]), 2)
        self.assertIn("release 'nonsense'", box.stderr)

    def test_build_runs_the_builder_and_lists_what_it_made(self) -> None:
        path = self.write_recipe()
        out = self.tmp / "out"
        out.mkdir()
        (out / "test-os.iso").write_bytes(b"iso" * 100)
        started: list[list[str]] = []

        class Stub:
            def __init__(self, argv: list[str], *, cwd: Path | None = None) -> None:
                self.argv, self.started_at = list(argv), os.path.getmtime(out) - 1
                self.cwd = cwd
                self.finish_code: int | None = None
                self.progress = os_builder.Progress()
                self.running = True

            def start(self) -> None:
                started.append(self.argv)
                self.finish_code, self.running = 0, False

            def drain(self, limit: int = 400) -> list[str]:
                return [] if self.running else ["I: done"]

            def stop(self) -> None:
                self.running = False

        with unittest.mock.patch.object(os_builder, "BuildRun", Stub), captured() as box:
            code = os_builder.main(["--build", str(path), "--timeout", "5"])
        self.assertEqual(code, 0, box.text)
        self.assertEqual(len(started), 1)
        self.assertNotIn("--dry-run", started[0])
        self.assertIn("disk image", box.stdout)
        self.assertIn("test-os.iso", box.stdout)
        self.assertIn("I: done", box.stdout)
        self.assertIn("no Docker and no live-build" if not shutil.which("docker") else "Docker", box.stdout)

    def test_build_can_keep_the_workspace(self) -> None:
        path = self.write_recipe()
        argv: list[str] = []

        class Stub:
            def __init__(self, received: list[str], *, cwd: Path | None = None) -> None:
                argv.extend(received)
                self.argv, self.started_at = list(received), 0.0
                self.finish_code: int | None = 0
                self.progress = os_builder.Progress(done=True)
                self.running = False

            def start(self) -> None:
                pass

            def drain(self, limit: int = 400) -> list[str]:
                return []

            def stop(self) -> None:
                pass

        with captured(), unittest.mock.patch.object(os_builder, "BuildRun", Stub):
            os_builder.main(["--build", str(path), "--keep-work", "--timeout", "3"])
        self.assertIn("--keep-work", argv)

    def test_build_reports_a_failing_builder(self) -> None:
        path = self.write_recipe()

        class Stub:
            def __init__(self, argv: list[str], *, cwd: Path | None = None) -> None:
                self.argv, self.started_at = list(argv), 0.0
                self.finish_code: int | None = None
                self.progress = os_builder.Progress(done=True, ok=False)
                self.running = True

            def start(self) -> None:
                self.finish_code, self.running = 2, False

            def drain(self, limit: int = 400) -> list[str]:
                return []

            def stop(self) -> None:
                pass

        with captured(), unittest.mock.patch.object(os_builder, "BuildRun", Stub):
            self.assertEqual(os_builder.main(["--build", str(path), "--timeout", "3"]), 2)

    def test_build_times_out_and_stops_the_child(self) -> None:
        path = self.write_recipe()
        stopped: list[bool] = []

        class Stub:
            def __init__(self, argv: list[str], *, cwd: Path | None = None) -> None:
                self.argv, self.started_at = list(argv), 0.0
                self.finish_code: int | None = None
                self.progress = os_builder.Progress()
                self.running = True

            def start(self) -> None:
                pass

            def drain(self, limit: int = 400) -> list[str]:
                return []

            def stop(self) -> None:
                stopped.append(True)
                self.running = False

        with captured() as box, unittest.mock.patch.object(os_builder, "BuildRun", Stub):
            self.assertEqual(os_builder.main(["--build", str(path), "--timeout", "1"]), 1)
        self.assertEqual(stopped, [True])
        self.assertIn("timed out", box.stderr)

    def test_build_survives_a_broken_builder_invocation(self) -> None:
        path = self.write_recipe()

        class Stub:
            def __init__(self, argv: list[str], *, cwd: Path | None = None) -> None:
                self.argv, self.started_at = list(argv), 0.0
                self.running = False
                self.finish_code: int | None = None
                self.progress = os_builder.Progress(done=True, ok=False)

            def start(self) -> None:
                raise os_builder.RecipeError("cannot start the builder: nope")

            def drain(self, limit: int = 400) -> list[str]:
                return []

            def stop(self) -> None:
                pass

        with captured() as box, unittest.mock.patch.object(os_builder, "BuildRun", Stub):
            self.assertEqual(os_builder.main(["--build", str(path)]), 2)
        self.assertIn("cannot start the builder", box.stderr)

    def test_build_stops_cleanly_on_ctrl_c(self) -> None:
        path = self.write_recipe()
        stopped: list[bool] = []

        class Stub:
            def __init__(self, argv: list[str], *, cwd: Path | None = None) -> None:
                self.argv, self.started_at = list(argv), 0.0
                self.running = True
                self.finish_code: int | None = None
                self.progress = os_builder.Progress()

            def start(self) -> None:
                raise KeyboardInterrupt

            def drain(self, limit: int = 400) -> list[str]:
                return []

            def stop(self) -> None:
                self.running = False

        with captured() as box, unittest.mock.patch.object(os_builder, "BuildRun", Stub):
            self.assertEqual(os_builder.main(["--build", str(path)]), 130)
        self.assertIn("interrupted", box.stderr)
        del stopped

    def test_gui_launch_without_tkinter_explains_the_alternative(self) -> None:
        with unittest.mock.patch.dict(sys.modules, {"tkinter": None}), captured() as box:
            code = os_builder.main([])
        self.assertEqual(code, 3)
        self.assertIn("no Tkinter", box.stderr)
        self.assertIn("--preview", box.stderr)
        self.assertIn("--build", box.stderr)

    def test_the_whole_cli_surface_is_documented_in_help(self) -> None:
        with captured() as box, self.assertRaises(SystemExit):
            os_builder.main(["--help"])
        for option in (
            "--list-presets",
            "--check",
            "--preview",
            "--build",
            "--wallpaper",
            "--new",
            "--preset",
        ):
            self.assertIn(option, box.stdout)


# --------------------------------------------------------------------------- #
# the Tk view, driven through a fake tkinter
# --------------------------------------------------------------------------- #
CREATED: list[object] = []


class _Widget:
    """A widget that accepts anything, remembers its options and its method calls."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.created_with = dict(kwargs)
        self.args = args
        self.configured: dict[str, object] = dict(self.created_with)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        CREATED.append(self)

    def configure(self, cnf: dict[str, object] | None = None, **kwargs: object) -> None:
        self.configured.update(kwargs)
        if isinstance(cnf, dict):
            self.configured.update(cnf)

    config = configure

    def cget(self, key: str) -> object:
        return self.created_with.get(key, "")

    def winfo_children(self) -> list[object]:
        return list(getattr(self, "_children", []))

    def destroy(self) -> None:
        self.calls.append(("destroy", ()))

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)

        def noop(*args: object, **kwargs: object) -> list[object]:
            self.calls.append((name, args, kwargs))
            return []  # Tk getters that are queried (get_children, selection) return a sequence

        return noop


class _Variable:
    def __init__(self, *_args: object, value: object = "", **_kwargs: object) -> None:
        self._value = value
        self.traces: list[object] = []

    def get(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._value = value

    def trace_add(self, mode: str, callback: object) -> str:
        self.traces.append(callback)
        return "trace-1"


class _Text(_Widget):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.inserts: list[tuple[object, ...]] = []
        self._content = ""

    def insert(self, index: object, text: object, *tags: object) -> None:
        self.inserts.append((index, text, tags))
        self._content += str(text)

    def get(self, start: str = "1.0", end: str = "end") -> str:
        return self._content

    def delete(self, *_args: object) -> None:
        self._content = ""
        self.inserts.clear()

    def tag_configure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def see(self, *_args: object) -> None:
        pass


class _Listbox(_Widget):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.items = []

    def insert(self, index: object, text: str) -> None:
        self.items.append(text)

    def delete(self, *args: object) -> None:
        self.items = []

    def curselection(self) -> tuple[int, ...]:
        return self.created_with.get("selection", ())  # type: ignore[return-value]

    def get(self, index: int) -> str:
        return self.items[index]


class _Notebook(_Widget):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.pages: list[object] = []

    def add(self, child: object, **kwargs: object) -> None:
        self.pages.append(kwargs.get("text", child))


class _Messagebox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.yes = False

    def _record(self, kind: str, title: str, message: str) -> bool:
        self.calls.append((kind, title, message))
        return self.yes

    def showinfo(self, title: str, message: str) -> bool:
        return self._record("info", title, message)

    def showwarning(self, title: str, message: str) -> bool:
        return self._record("warning", title, message)

    def showerror(self, title: str, message: str) -> bool:
        return self._record("error", title, message)

    def askyesno(self, title: str, message: str) -> bool:
        return self._record("yesno", title, message)


@contextlib.contextmanager
def fake_tkinter() -> Iterator[dict[str, object]]:
    """Import os_builder_gui against a stand-in tkinter, then undo the whole thing."""
    names = ("tkinter", "tkinter.ttk", "tkinter.font", "tkinter.filedialog", "tkinter.messagebox")
    saved = {name: sys.modules.get(name) for name in names}
    saved_gui = sys.modules.pop("os_builder_gui", None)
    CREATED.clear()

    tkinter = ModuleType("tkinter")
    tkinter.TclError = type("TclError", (Exception,), {})
    tkinter.Variable = _Variable
    tkinter.StringVar = _Variable
    tkinter.BooleanVar = type("BooleanVar", (_Variable,), {"get": lambda self: bool(self._value)})
    # Each name gets its own subclass, so `isinstance(child, INTERACTIVE)` in the code
    # under test means the same thing it means with real Tk.
    for name, klass in (
        ("Text", _Text),
        ("Listbox", _Listbox),
        ("Canvas", type("Canvas", (_Widget,), {})),
        ("Entry", type("Entry", (_Widget,), {})),
        ("Button", type("Button", (_Widget,), {})),
        ("Frame", type("Frame", (_Widget,), {})),
        ("Label", type("Label", (_Widget,), {})),
        ("Menu", type("Menu", (_Widget,), {})),
        ("Toplevel", type("Toplevel", (_Widget,), {})),
        ("PanedWindow", type("PanedWindow", (_Widget,), {})),
        ("PhotoImage", type("PhotoImage", (_Widget,), {})),
    ):
        setattr(tkinter, name, klass)

    class _Tk(_Widget):
        def after(self, delay: int, func: object = None) -> str:
            self.calls.append(("after", (delay,), {"func": func}))
            return "job-1"

        def after_cancel(self, job: object) -> None:
            self.calls.append(("after_cancel", (job,), {}))

        def mainloop(self) -> None:
            self.calls.append(("mainloop", ()))

        def bind_all(self, sequence: str, handler: object) -> None:
            self.bindings[sequence] = handler

        def clipboard_append(self, text: str) -> None:
            self.clipboard = text

    _Tk.bindings = {}  # type: ignore[attr-defined]
    tkinter.Tk = _Tk

    ttk = ModuleType("tkinter.ttk")
    for name in (
        "Frame",
        "Label",
        "Button",
        "Checkbutton",
        "Radiobutton",
        "Combobox",
        "Progressbar",
        "Scrollbar",
        "Treeview",
        "Spinbox",
    ):
        setattr(ttk, name, type(name, (_Widget,), {}))
    ttk.Notebook = _Notebook

    class _Style(_Widget):
        def theme_use(self, *_args: object) -> None:
            pass

        def map(self, *_args: object, **_kwargs: object) -> None:
            pass

    ttk.Style = _Style

    font = ModuleType("tkinter.font")
    font.nametofont = lambda name: _Widget()

    filedialog = ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **kwargs: ""
    filedialog.asksaveasfilename = lambda **kwargs: ""
    filedialog.askdirectory = lambda **kwargs: ""

    box = _Messagebox()
    message = ModuleType("tkinter.messagebox")
    for name in ("showinfo", "showwarning", "showerror", "askyesno"):
        setattr(message, name, getattr(box, name))

    tkinter.ttk, tkinter.filedialog, tkinter.messagebox, tkinter.font = ttk, filedialog, message, font
    sys.modules.update(
        {
            "tkinter": tkinter,
            "tkinter.ttk": ttk,
            "tkinter.font": font,
            "tkinter.filedialog": filedialog,
            "tkinter.messagebox": message,
        }
    )
    import os_builder_gui

    importlib.reload(os_builder_gui)
    try:
        yield {
            "gui": os_builder_gui,
            "tk": tkinter,
            "filedialog": filedialog,
            "messagebox": box,
            "tk_class": _Tk,
        }
    finally:
        sys.modules.pop("os_builder_gui", None)
        if saved_gui is not None:
            sys.modules["os_builder_gui"] = saved_gui
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class GuiTests(TempDirCase):
    def test_the_window_is_built_from_the_model(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Gui OS", accent="#39d353"))
            self.assertEqual(len(gui.notebook.pages), 5)
            self.assertEqual(
                gui.notebook.pages, ["identity", "software", "appearance", "files & hooks", "build"]
            )
            for key in (
                "name",
                "tagline",
                "hostname",
                "motd",
                "notes",
                "release",
                "architecture",
                "compression",
                "container",
                "preset",
                "accent",
                "wallpaper_style",
                "wallpaper_size",
                "with_wallpaper",
                "with_firmware",
                "output_dir",
                "work_dir",
                "cache_dir",
                "boot_test",
                "keep_work",
                "skip_checks",
            ):
                self.assertIn(key, gui.vars, f"{key} has no widget, so the GUI cannot edit it")
            listed = {"packages", "files", "hooks"}
            unaddressed = set(gui.model.recipe.__dataclass_fields__) - set(gui.vars) - listed
            self.assertEqual(unaddressed, set(), "these recipe fields have no widget")
            gui._on_close()

    def test_widget_edits_flow_into_the_model(self) -> None:
        with fake_tkinter() as env:
            model = self.model()
            gui = env["gui"].BuilderGUI(model=model)
            for key, value in (
                ("name", "Typed Name"),
                ("tagline", "edited"),
                ("output_dir", str(self.tmp / "other")),
            ):
                var = gui.vars[key]
                var.set(value)
                var.traces[0]("write", "", "")
                self.assertEqual(getattr(model.recipe, key), value)
            motd = gui.vars["motd"]
            motd.insert("1.0", "line one\nline two")
            gui._push_text("motd", motd)
            self.assertEqual(model.recipe.motd, "line one\nline two")
            gui._on_close()

    def test_tracing_is_suppressed_while_the_view_is_being_synced(self) -> None:
        with fake_tkinter() as env:
            model = self.model(name="Steady")
            gui = env["gui"].BuilderGUI(model=model)
            gui._syncing = True
            gui.vars["name"].set("ignored")
            gui.vars["name"].traces[0]("write", "", "")
            self.assertEqual(model.recipe.name, "Steady")
            gui._on_close()

    def test_the_model_change_hook_updates_header_and_status(self) -> None:
        with fake_tkinter() as env:
            model = self.model(name="Header", packages=["htop"])
            gui = env["gui"].BuilderGUI(model=model)
            model.set(name="Renamed")
            self.assertIn("Renamed", str(gui.flavour.configured.get("text", "")))
            self.assertIn("ready", str(gui.status.configured.get("text", "")))
            self.assertIn("recipe: unsaved", str(gui.recipe_label.configured.get("text", "")))
            gui._on_close()

    def test_package_toggles_are_reflected_in_the_list(self) -> None:
        with fake_tkinter() as env:
            model = self.model(preset="minimal")
            gui = env["gui"].BuilderGUI(model=model)
            before = list(gui.package_list.items)
            model.toggle_package("vim")
            self.assertIn("vim", gui.package_list.items)
            self.assertEqual(model.recipe.packages, ["vim"])
            self.assertIn("htop", gui.package_list.items, "preset packages are shown too")
            self.assertNotEqual(before, gui.package_list.items)
            gui._on_close()

    def test_log_lines_are_coloured_by_content(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            for line, expected in (
                ("os-maker: error: nothing works", "err"),
                ("os-maker: warning: no kernel", "warn"),
                ("os-builder: recipe saved to /tmp/x.json", "ok"),
                ("I: Bootstrap stage", "info"),
                ("Collecting the image", "info"),
            ):
                gui._log(line)
                _index, _text, tags = gui.log.inserts[-1]
                self.assertEqual(tags, (expected,), line)
            gui._log("anything at all", tag="ok")
            self.assertEqual(gui.log.inserts[-1][-1], ("ok",))
            gui._on_close()

    def test_polling_moves_the_bar_and_stops_when_finished(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())

            class FakeRun:
                def __init__(self) -> None:
                    self.running = True
                    self.finish_code: int | None = None
                    self.elapsed = 12.0
                    self.progress = os_builder.Progress(label="bootstrapping Debian", fraction=0.4)
                    self._lines = ["I: Bootstrap", "hybrid MBR"]

                def drain(self, limit: int = 400) -> list[str]:
                    out, self._lines = self._lines, []
                    return out

                def stop(self) -> None:
                    pass

            run = FakeRun()
            gui.model.run = run  # type: ignore[assignment]
            gui._poll()
            self.assertEqual(gui.progress.configured.get("value"), 0.4)
            self.assertIn("I: Bootstrap", str(gui.log.inserts[-2][1]))
            self.assertIn("bootstrapping", str(gui.status.configured.get("text", "")))
            self.assertEqual(gui.poll_job, "job-1", "a running build must schedule another poll")
            run.running = False
            run.finish_code = 0
            gui._poll()
            self.assertIsNone(gui.poll_job)
            self.assertIn("12s", str(gui.status.configured.get("text", "")))
            gui._on_close()

    def test_a_successful_build_announces_the_iso(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            iso = os_builder.Artifact(
                self.tmp / "aurora.iso", "disk image", 3 * 1024**2, os.path.getmtime(self.tmp)
            )

            class Done:
                running = False
                finish_code = 0
                elapsed = 5.0
                progress = os_builder.Progress(label="build finished", done=True, ok=True, fraction=1.0)

                def drain(self, limit: int = 400) -> list[str]:
                    return []

                def stop(self) -> None:
                    pass

            gui.model.run = Done()  # type: ignore[assignment]
            with unittest.mock.patch.object(gui.model, "artifacts", return_value=[iso]):
                gui._poll()
            self.assertTrue(
                any("aurora.iso" in message for _kind, _title, message in env["messagebox"].calls)
            )
            inserts = [call for call in gui.artifacts.calls if call[0] == "insert"]
            self.assertEqual(len(inserts), 1)
            self.assertIn("3.0 MiB", str(inserts[0]))
            self.assertIn("built", "".join(str(item[1]) for item in gui.log.inserts))
            gui._on_close()

    def test_a_build_with_no_iso_is_reported(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())

            class Done:
                running = False
                finish_code = 0
                elapsed = 1.0
                progress = os_builder.Progress(label="build finished", done=True, ok=True)

                def drain(self, limit: int = 400) -> list[str]:
                    return []

                def stop(self) -> None:
                    pass

            gui.model.run = Done()  # type: ignore[assignment]
            gui._poll()
            self.assertIn("left no ISO", "".join(str(item[1]) for item in gui.log.inserts))
            gui._on_close()

    def test_start_build_refuses_a_recipe_with_problems(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="", release="nope"))
            gui._start_build()
            self.assertTrue(any(kind == "error" for kind, _t, _m in env["messagebox"].calls))
            self.assertIn("to fix", str(gui.status.configured.get("text", "")))
            gui._on_close()

    def test_start_build_clears_the_log_and_locks_the_form(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._log("stale line")
            run = unittest.mock.MagicMock()
            run.argv = ["python", "build_os.py"]
            with unittest.mock.patch.object(gui.model, "start_build", return_value=run) as start:
                gui._start_build()
            self.assertTrue(start.called)
            self.assertEqual(gui.build_button.configured.get("state"), "disabled")
            self.assertEqual(gui.stop_button.configured.get("state"), "normal")
            self.assertIn("starting:", "".join(str(item[1]) for item in gui.log.inserts))
            self.assertNotIn("stale line", "".join(str(item[1]) for item in gui.log.inserts))
            gui._set_running(False)
            self.assertEqual(gui.build_button.configured.get("state"), "normal")
            gui.model.run = None
            gui._on_close()

    def test_stop_only_acts_on_a_running_build(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._stop()  # nothing running: must not raise
            run = unittest.mock.MagicMock()
            run.running = True
            gui.model.run = run
            gui._stop()
            run.stop.assert_called_once()
            self.assertIn("asked the builder to stop", "".join(str(item[1]) for item in gui.log.inserts))
            gui.model.run = None
            gui._on_close()

    def test_accent_changes_restyle_the_window(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(accent="#4f9cf9"))
            gui.model.set(accent="#ff6ac1")
            self.assertEqual(gui.accent, "#ff6ac1")
            gui.model.set(accent="not a colour")
            self.assertEqual(gui.accent, "#ff6ac1", "a broken colour must not wreck the theme")
            gui._on_close()

    def test_the_preview_is_debounced_and_drawn(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._queue_preview()
            self.assertEqual(gui.preview_job, "job-1")
            gui._queue_preview()
            self.assertTrue(
                any(name == "after_cancel" for name, *_rest in gui.root.calls),
                "the old job must be cancelled",
            )
            gui._render_preview()
            self.assertIsNone(gui.preview_job)
            self.assertTrue((Path(gui.temp_dir) / "preview.png").is_file())
            self.assertIn("preview 384x216", str(gui.preview_note.configured.get("text", "")))
            gui._on_close()

    def test_a_broken_wallpaper_setting_does_not_kill_the_preview(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(accent="#00ff00"))
            gui.model.recipe.accent = "#gggggg"
            gui._render_preview()
            self.assertIn("preview unavailable", str(gui.preview_note.configured.get("text", "")))
            gui._on_close()

    def test_recipe_save_and_load_use_the_dialogs(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Dialog OS"))
            target = self.tmp / "gui-recipe.json"
            env["filedialog"].asksaveasfilename = lambda **kwargs: str(target)
            gui._save()
            self.assertTrue(target.is_file())
            self.assertIn("saved", "".join(str(item[1]) for item in gui.log.inserts))
            self.assertEqual(os_builder.Recipe.from_file(target).name, "Dialog OS")
            self.assertIn("recipe: gui-recipe.json", str(gui.recipe_label.configured.get("text", "")))
            gui.model.recipe = os_builder.Recipe()
            env["filedialog"].askopenfilename = lambda **kwargs: str(target)
            gui._open()
            self.assertEqual(gui.model.recipe.name, "Dialog OS")
            gui._on_close()

    def test_save_as_always_asks_for_a_path(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Twice"))
            first = self.tmp / "one.json"
            second = self.tmp / "two.json"
            env["filedialog"].asksaveasfilename = lambda **kwargs: str(first)
            gui._save()
            gui._save()
            self.assertTrue(second.parent.is_dir())
            env["filedialog"].asksaveasfilename = lambda **kwargs: str(second)
            gui._save(as_file=True)
            self.assertTrue(first.is_file() and second.is_file())
            self.assertEqual(gui.model.path, second)
            gui._on_close()

    def test_a_save_failure_is_reported_in_a_dialog(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            blocked = self.tmp / "blocked.json"
            blocked.mkdir()
            env["filedialog"].asksaveasfilename = lambda **kwargs: str(blocked)
            gui._save()
            self.assertTrue(any(kind == "error" for kind, _t, _m in env["messagebox"].calls))
            gui._on_close()

    def test_a_broken_recipe_fails_with_a_dialog_not_a_traceback(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            junk = self.tmp / "junk.json"
            junk.write_text("{oops", encoding="utf-8")
            env["filedialog"].askopenfilename = lambda **kwargs: str(junk)
            gui._open()
            self.assertTrue(any("not valid JSON" in message for _k, _t, message in env["messagebox"].calls))
            gui._on_close()

    def test_cancelled_dialogs_change_nothing(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Kept"))
            for action in (
                gui._save,
                gui._open,
                gui._choose_output,
                gui._add_file,
                gui._add_hook,
                gui._export_wallpaper,
            ):
                action()
            self.assertEqual(gui.model.recipe.name, "Kept")
            self.assertEqual(gui.model.recipe.files, [])
            self.assertEqual(gui.model.recipe.hooks, [])
            self.assertEqual(gui.model.recipe.output_dir, str(self.tmp / "out"))
            gui._on_close()

    def test_adding_a_file_records_the_destination(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            source = self.tmp / "note.txt"
            source.write_text("hi\n", encoding="utf-8")
            env["filedialog"].askopenfilename = lambda **kwargs: str(source)
            with unittest.mock.patch.object(env["gui"], "_prompt", return_value="/root/note.txt"):
                gui._add_file()
            self.assertEqual(gui.model.recipe.files, [f"{source.as_posix()}:/root/note.txt"])
            self.assertIn("will copy", "".join(str(item[1]) for item in gui.log.inserts))
            gui._remove_file()  # nothing selected: no change
            self.assertEqual(len(gui.model.recipe.files), 1)
            gui.model.remove_file(0)
            self.assertEqual(gui.model.recipe.files, [])
            gui._add_hook()
            self.assertEqual(gui.model.recipe.hooks, [str(source)])
            gui._remove_hook()  # nothing selected: the entry stays
            self.assertEqual(gui.model.recipe.hooks, [str(source)])
            gui.model.remove_hook(0)
            self.assertEqual(gui.model.recipe.hooks, [])
            gui._on_close()

    def test_a_cancelled_prompt_adds_nothing(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            source = self.tmp / "note.txt"
            source.write_text("hi\n", encoding="utf-8")
            env["filedialog"].askopenfilename = lambda **kwargs: str(source)
            with unittest.mock.patch.object(env["gui"], "_prompt", return_value=""):
                gui._add_file()
            self.assertEqual(gui.model.recipe.files, [])
            gui._on_close()

    def test_the_package_entry_adds_and_the_preset_protects(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(preset="minimal"))
            gui.add_package.set("htop  vim")
            gui._add_packages()
            self.assertIn("vim", gui.model.recipe.packages)
            self.assertEqual(str(gui.add_package.get()), "")
            gui.package_list = _Listbox(selection=(0,))  # type: ignore[call-arg]
            gui.package_list.items = ["htop"]
            gui._remove_package()
            self.assertTrue(
                any("preset" in message for _kind, _title, message in env["messagebox"].calls),
                "removing a preset package needs an explanation",
            )
            self.assertIn("htop", gui.model.recipe.packages)
            gui.package_list.items = ["vim"]
            gui._remove_package()
            self.assertNotIn("vim", gui.model.recipe.packages)
            gui.package_list = _Listbox()  # nothing selected
            gui.package_list.items = ["vim"]
            gui._remove_package()
            gui._on_close()

    def test_the_listbox_shows_every_effective_package(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(preset="minimal", packages=["extra-one"]))
            self.assertEqual(gui.package_list.items[:1], ["htop"])
            self.assertIn("extra-one", gui.package_list.items)
            gui._on_close()

    def test_checksum_verification_needs_an_artifact(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._verify()
            self.assertTrue(
                any("Nothing to verify" in message for _k, _t, message in env["messagebox"].calls)
            )
            gui._on_close()

    def test_verify_uses_the_selected_iso(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            out = self.tmp / "out"
            out.mkdir()
            iso = out / "os.iso"
            iso.write_bytes(b"payload")
            gui.artifacts.selection = lambda: ("I001",)  # type: ignore[method-assign]

            def item(_index: object, key: str) -> str:
                return "os.iso" if key == "text" else ""

            gui.artifacts.item = item  # type: ignore[method-assign]
            with unittest.mock.patch.object(
                os_builder, "verify_checksum", return_value=(True, "checksum matches")
            ) as check:
                gui._verify()
            self.assertEqual(check.call_args[0][0], iso)
            self.assertTrue(any("matches" in message for _k, _t, message in env["messagebox"].calls))
            gui._on_close()

    def test_the_artifact_table_is_populated(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            out = self.tmp / "out"
            out.mkdir()
            (out / "x.iso").write_bytes(b"z" * 10)
            (out / "x.iso.sha256").write_text("deadbeef\n", encoding="utf-8")
            gui._refresh_artifacts()
            kinds = [call for call in gui.artifacts.calls if call[0] == "insert"]
            self.assertEqual(len(kinds), 2)
            self.assertIn("2 file(s)", str(gui.build_note.configured.get("text", "")))
            gui._on_close()

    def test_the_log_window_needs_a_file(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._open_log()
            self.assertTrue(any("No build log" in message for _k, _t, message in env["messagebox"].calls))
            out = self.tmp / "out"
            out.mkdir()
            (out / "build.log").write_text("hello from the builder\n", encoding="utf-8")
            gui._open_log()
            window = [widget for widget in CREATED if isinstance(widget, env["tk"].Toplevel)]  # type: ignore[attr-defined]
            self.assertTrue(window)
            gui._on_close()

    def test_reveal_reports_a_missing_file_opener(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            with unittest.mock.patch.object(
                env["gui"].subprocess, "Popen", side_effect=OSError("no xdg-open here")
            ):
                gui._reveal_output()
            self.assertIn("could not open", "".join(str(item[1]) for item in gui.log.inserts))
            gui._on_close()

    def test_the_output_folder_is_created_on_demand(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(output_dir=str(self.tmp / "deep" / "out")))
            with unittest.mock.patch.object(env["gui"].subprocess, "Popen") as popen:
                gui._reveal_output()
            self.assertTrue((self.tmp / "deep" / "out").is_dir())
            self.assertTrue(popen.called)
            gui._on_close()

    def test_show_command_and_the_clipboard(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._show_argv()
            self.assertIn("build_os.py", "".join(str(item[1]) for item in gui.log.inserts))
            gui._copy("echo hi")
            self.assertEqual(gui.root.clipboard, "echo hi")
            gui._on_close()

    def test_apply_preset_button_and_keep_appearance(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Mine", accent="#abcdef", preset="minimal"))
            gui.vars["preset"].set("gaming")
            buttons = [widget for widget in CREATED if widget.created_with.get("text") == "apply preset"]
            self.assertTrue(buttons)
            buttons[0].created_with["command"]()
            self.assertEqual(gui.model.recipe.preset, "gaming")
            self.assertEqual(gui.model.recipe.name, "Mine")
            gui._on_close()

    def test_the_new_recipe_dialog_starts_from_a_preset(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(name="Before", accent="#111111", preset="minimal"))
            gui._new_recipe()
            start = [widget for widget in CREATED if widget.created_with.get("text") == "start here"]
            self.assertTrue(start)
            start[0].created_with["command"]()
            self.assertEqual(gui.model.recipe.preset, "minimal")
            self.assertEqual(gui.model.recipe.accent, "#111111", "keep my colours is on by default")
            self.assertIsNone(gui.model.path, "a new recipe is not saved yet")
            gui._on_close()

    def test_the_accent_swatches_set_the_colour(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(accent="#4f9cf9"))
            gui.accent_buttons["#39d353"].created_with["command"]()
            self.assertEqual(gui.model.recipe.accent, "#39d353")
            gui._on_close()

    def test_menubar_shortcuts_are_all_wired(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            bindings = gui.root.bindings
            for sequence in (
                "Control-s",
                "Control-S",
                "Control-o",
                "Control-n",
                "Control-p",
                "Control-b",
                "Control-q",
                "Escape",
            ):
                self.assertIn(sequence, bindings)
            self.assertTrue(all(callable(bindings[key]) for key in bindings))
            bindings["Escape"](None)  # nothing running, so this must be harmless
            bindings["Control-s"](None)  # the save dialog cancels in this fixture
            gui._on_close()

    def test_menu_entries_exist(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            menus = [widget for widget in CREATED if isinstance(widget, env["tk"].Menu)]  # type: ignore[attr-defined]
            self.assertTrue(menus, "the window should have a menu bar")
            labels = " ".join(
                str(call[2].get("label"))
                for widget in menus
                for call in widget.calls
                if call[0] == "add_command"
            )
            for wanted in ("Open recipe...", "Save recipe", "Generate wallpaper PNG", "About OS Builder"):
                self.assertIn(wanted, labels)
            gui._on_close()

    def test_about_and_shortcuts_are_informational(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui._about()
            gui._shortcuts()
            self.assertEqual(len(env["messagebox"].calls), 2)
            self.assertIn("Ctrl+B", "".join(message for _k, _t, message in env["messagebox"].calls))
            gui._on_close()

    def test_wallpaper_export_writes_the_shipped_size(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model(wallpaper_size="200x120"))
            target = self.tmp / "big.png"
            env["filedialog"].asksaveasfilename = lambda **kwargs: str(target)
            gui._export_wallpaper()
            self.assertEqual(os_builder.read_png_size(target), (200, 120))
            gui._on_close()

    def test_dry_run_reports_what_the_builder_says(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            with unittest.mock.patch.object(
                gui.model, "preview", return_value=(0, "Planned build inside debian:trixie")
            ):
                gui._dry_run()
            joined = "".join(str(item[1]) for item in gui.log.inserts)
            self.assertIn("Planned build inside debian:trixie", joined)
            self.assertIn("exited 0", joined)
            self.assertEqual(gui.preview_button.configured.get("state"), "normal")
            gui._on_close()

    def test_dry_run_is_refused_while_building(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            gui.model.run = unittest.mock.MagicMock(running=True)
            gui._dry_run()
            self.assertTrue(
                any("A build is running" in message for _k, _t, message in env["messagebox"].calls)
            )
            gui.model.run = None
            gui._on_close()

    def test_run_enters_the_mainloop_and_cleans_up(self) -> None:
        with fake_tkinter() as env:
            model = self.model()
            gui = env["gui"].BuilderGUI(model=model)
            scratch = model.scratch
            assert scratch is not None
            gui.run()
            self.assertTrue(any(name == "mainloop" for name, *_rest in gui.root.calls))
            self.assertFalse(scratch.exists(), "closing must not leave a scratch directory behind")

    def test_closing_asks_before_killing_a_running_build(self) -> None:
        with fake_tkinter() as env:
            gui = env["gui"].BuilderGUI(model=self.model())
            run = unittest.mock.MagicMock()
            run.running = True
            gui.model.run = run
            env["messagebox"].yes = False
            gui._on_close()  # "keep building" -> nothing is stopped or destroyed
            run.stop.assert_not_called()
            self.assertTrue(any(kind == "yesno" for kind, _t, _m in env["messagebox"].calls))
            gui.model.run = None
            gui._on_close()

    def test_set_state_tree_reaches_nested_widgets_only(self) -> None:
        with fake_tkinter() as env:
            module = env["gui"]
            tkinter = env["tk"]
            outer = _Notebook()
            entry = tkinter.Entry(text="an entry")
            plain = _Notebook()
            nested = tkinter.Button(text="deep")
            plain._children = [nested]
            outer._children = [entry, plain]
            module._set_state_tree(outer, "disabled")
            self.assertEqual(entry.configured.get("state"), "disabled")
            self.assertIsNone(plain.configured.get("state"), "a container is not interactive itself")
            self.assertEqual(nested.configured.get("state"), "disabled", "nesting must be walked")
            self.assertIsNone(outer.configured.get("state"))
            gui = module.BuilderGUI(model=self.model())
            gui._set_running(True)
            self.assertEqual(gui.build_button.configured.get("state"), "disabled")
            self.assertEqual(gui.log.configured.get("state"), "disabled", "the log stays readable")
            gui._set_running(False)
            self.assertEqual(gui.build_button.configured.get("state"), "normal")
            gui._on_close()


class PromptTests(TempDirCase):
    """The themed one-line prompt the file picker uses for the destination path."""

    def test_the_prompt_returns_empty_until_a_button_is_pressed(self) -> None:
        with fake_tkinter() as env:
            answer = env["gui"]._prompt(_Widget(), "Where in the image?", "note.txt", "/root/note.txt")
            self.assertEqual(answer, "", "wait_window() is a no-op in the fake, so nothing was accepted")

    def test_pressing_ok_returns_the_typed_text(self) -> None:
        with fake_tkinter() as env:
            tkinter = env["tk"]

            class _Dialog(_Widget):
                """A dialog that clicks its own ok button when Tk would block."""

                def wait_window(self) -> None:
                    for widget in CREATED:
                        if isinstance(widget, _Widget) and widget.created_with.get("text") == "ok":
                            widget.created_with["command"]()
                            return

            tkinter.Toplevel = _Dialog
            try:
                answer = env["gui"]._prompt(_Widget(), "Where in the image?", "note.txt", "/root/note.txt")
                self.assertEqual(answer, "/root/note.txt")
                self.assertEqual(CREATED[-1].created_with.get("text"), "cancel")
            finally:
                tkinter.Toplevel = _Widget


class RealTkTests(unittest.TestCase):
    """With a real Tk and a display, prove the window can actually be built.

    Opt in, because CI runners have no window system: ``OS_BUILDER_REAL_TK=1``.
    """

    def test_the_window_builds_with_the_real_toolkit(self) -> None:
        if os.environ.get("OS_BUILDER_REAL_TK") != "1":
            self.skipTest("set OS_BUILDER_REAL_TK=1 to exercise the real Tk (needs a display)")
        try:
            import tkinter
        except ImportError:
            self.skipTest("no tkinter on this interpreter")
        module = importlib.import_module("os_builder_gui")
        gui = module.BuilderGUI(os_builder.Recipe.for_preset("minimal"))
        self.addCleanup(gui._on_close)
        gui.model.set(name="Real Tk")
        gui._render_preview()
        self.assertTrue((Path(gui.temp_dir) / "preview.png").is_file())
        self.assertTrue(tkinter.TkVersion >= 8.6)


if __name__ == "__main__":
    unittest.main()
