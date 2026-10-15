# What improved, and what proves it

Every line below is a behaviour that exists in this tree and is checked by a test that runs
real code: the generated hook is executed against a fake rootfs, the JSON is parsed, the PNG
is decoded. Nothing is listed for volume. If it is not in the table, it was not done.

Verify the whole ledger yourself:

```bash
python3 -m unittest discover -s tests -t .      # every claim below is one of these tests
python3 os_builder.py --explain                 # what each recipe field turns into
python3 os_builder.py --batch examples/         # the shipped recipes all validate
python3 -m mypy && ruff check .                 # types and lint, no exceptions made
```

This round is **61** items. The count is not a target and the number is not the
point; the middle column is.

| # | Area | What changed | Where it is proved |
| --- | --- | --- | --- |
| 1 | recipe | `locale` writes /etc/default/locale and uncomments the line in /etc/locale.gen | `HookExecutionTests.test_the_hook_sets_the_clock_the_locale_and_the_keymap` |
| 2 | recipe | `keyboard` writes /etc/default/keyboard (XKB) and /etc/vconsole.conf, the latter only where systemd-vconsole-setup could read it | the same test; the harness comment names the guard |
| 3 | recipe | `timezone` symlinks /etc/localtime and writes /etc/timezone, and does nothing when the zoneinfo file is absent | `test_the_hook_sets_the_clock_the_locale_and_the_keymap` |
| 4 | recipe | `kernel` is a choice (auto / generic / cloud / realtime / none) mapped to a per-architecture package | `SystemFieldTests.test_the_kernel_becomes_a_package` |
| 5 | recipe | `grub_timeout` edits GRUB_TIMEOUT in place and hides the menu at 0; it never rewrites the file for a recipe that said nothing | `test_the_boot_menu_timeout_is_written_and_hidden_at_zero` |
| 6 | recipe | `apt_sources` land in /etc/apt/sources.list.d/os-maker.list, one vetted deb line at a time | `test_extra_sources_and_removals_reach_the_image` |
| 7 | recipe | `remove_packages` become a guarded apt-get purge --auto-remove, proven against a fake apt-get on PATH | the same test: the fake records its argv and the test reads the log back |
| 8 | recipe | `picture_options` reaches both desktops: dconf by name, XFCE by its integer code | `test_the_background_fit_reaches_both_desktops` |
| 9 | recipe | `version` becomes VERSION_ID and part of PRETTY_NAME; `author` becomes OS_MAKER_MAINTAINER; `home_url` becomes HOME_URL and SUPPORT_URL | `SystemFieldTests.test_os_release_uses_the_recipes_own_identity` |
| 10 | recipe | a blank `home_url` keeps the project URL instead of inventing one | the same test's last assertion |
| 11 | recipe | `with_branding_record` adds /etc/os-maker.json: a machine-readable card with the digest, inside the image | `test_make_branding_writes_the_record_only_when_asked` |
| 12 | recipe | `show_wordmark` and `motd_art` are switches: turn them off and the text is gone | `WallpaperTests.test_the_name_is_stamped_unless_asked_not_to_be` |
| 13 | recipe | `wallpaper_seed` picks a variation of the organic styles; the same seed draws the same picture | `WallpaperSeedTests.test_rolling_the_seed_rolls_the_picture` |
| 14 | recipe | every new field is validated with a message that names the field and shows the shape it wanted | SystemFieldTests.test_nonsense_values_are_named (15 subTests) |
| 15 | recipe | the loader refuses the wrong type for each new field instead of coercing it | `test_the_wrong_type_of_every_new_field_is_refused` |
| 16 | recipe | a number that is not a number says so: 'boot_timeout' must be a whole number of seconds | the type branch in SystemFieldTests, and ValidationTests for the older fields |
| 17 | recipe | recipes written before these fields existed still load and mean exactly what they meant | `test_the_defaults_are_unchanged_for_an_old_recipe` |
| 18 | recipe | Recipe.digest(): one 64-hex identity for the settings that reach the image, stable across save and load | `test_the_digest_is_stable_and_settings_sensitive` |
| 19 | recipe | pretty_name and kernel_package are properties, so nothing recomputes them differently elsewhere | exercised by the os-release test and the --packages test |
| 20 | wallpaper | eleven generated styles: gradient, sunset, rays, grid, waves, aurora, mesh, dots, stripes, spiral, nebula | `WallpaperTests.test_all_styles_render_at_the_requested_size` |
| 21 | wallpaper | every style is reachable from the CLI too, at the recipe's own size | `WallpaperSeedTests.test_every_style_renders_from_the_command_line` |
| 22 | wallpaper | colours stay in gamut (0-255) and no style renders a flat image | `test_pixels_stay_in_range_and_are_not_flat` |
| 23 | wallpaper | one renderer for the GUI preview and the shipped PNG, so the two cannot drift | render_wallpaper_png is the single entry point both call |
| 24 | errors | a well-shaped but too-small wallpaper_size says 'must be between 64x64 and 4096x4096' instead of repeating the format | `SystemFieldTests.test_a_tiny_wallpaper_size_says_so_exactly` |
| 25 | errors | --explain grew the row it was missing (output_dir) and its columns can no longer collide | `RecipeToolTests.test_explain_covers_every_field_the_gui_edits` |
| 26 | errors | an unknown preset in --preset-info prints the names that do exist | `test_preset_info_prints_one_preset_in_full` |
| 27 | cli | --json on --check, --preview, --list-presets and --build: one parseable object, exit code unchanged | CommandLineReportTests (four tests that call json.loads on the output) |
| 28 | cli | --preview --json reports the command with --dry-run, because that is what preview means | `test_preview_as_json_does_not_run_the_builder` |
| 29 | cli | the JSON for a broken recipe still carries the reasons, and still exits 2 | `test_check_as_json_carries_the_reasons_too` |
| 30 | cli | --quiet swallows the build log but keeps draining the pipe, so a long build cannot deadlock on a full buffer | echo_lines' contract, pinned by the stubbed-build tests |
| 31 | cli | --tail N repeats the last N lines when a quiet build fails | `test_quiet_build_shows_the_tail_when_the_builder_fails` |
| 32 | cli | --stall-notice SECONDS says the builder has gone quiet, once, without changing the outcome | headless_build's stall branch, driven by the same stub harness |
| 33 | cli | hint_from_log turns a known failure into one actionable sentence (disk, Docker, permissions, apt, boot test) | `RecipeToolTests.test_a_hint_is_offered_for_a_known_failure` |
| 34 | cli | --fix FILE prints the canonical recipe; it writes only when --out is given | `test_fix_prints_the_changes_and_touches_nothing, test_fix_writes_a_canonical_file_when_asked` |
| 35 | cli | the tidy rules: strip spaces, lower-case the accent, canonical WxH, dedupe lists in order, end motd with a newline, drop a hostname that repeats the derived one | `test_normalise_tidies_without_changing_the_meaning` |
| 36 | cli | a clean recipe is reported as clean instead of rewritten for sport | `test_normalise_has_nothing_to_say_about_a_clean_recipe` |
| 37 | cli | --fix cannot invent a recipe that would not load: every change is rebuilt through the loader's checks | revalidate(), plus test_fix_still_reports_a_recipe_that_cannot_build |
| 38 | cli | --diff A B prints what differs, exits 1 when they do and 0 when they do not | `test_diff_names_both_sides_and_returns_one` |
| 39 | cli | --batch DIR checks a whole folder and returns the worst result; an empty or missing folder is an error, not a success | `test_batch_checks_a_folder_and_returns_the_worst, test_batch_refuses_a_folder_without_recipes` |
| 40 | cli | --gui-check answers the first question on a new machine: is there a Tk here, and if not what do I use instead | `test_gui_check_tells_the_truth_about_this_machine` |
| 41 | cli | OS_BUILDER_OUTPUT / _WORK / _CACHE / _CONTAINER / _TIMEOUT fill only what the recipe left blank; a silly timeout is ignored | `test_environment_defaults_fill_only_what_the_recipe_left_blank, test_a_silly_timeout_in_the_environment_is_ignored` |
| 42 | cli | --help groups the machine flags under 'for scripts' and the file tools under 'recipe tools' | the argument groups in build_parser, checked against the printed usage |
| 43 | cli | headless_build reads its optional flags with getattr, so a caller with a hand-built Namespace keeps working | the four pre-existing CliTests pass untouched |
| 44 | gui | a sixth tab, 'system', for the settings that change the machine rather than the ISO | GuiTests.test_the_window_is_built_from_the_model pins the tab order |
| 45 | gui | every recipe field has a widget, and the suite fails if a new field arrives without one | the same test's unaddressed-fields assertion |
| 46 | gui | 'roll the seed' changes the model, the entry and the preview in one click | `GuiSystemTabTests.test_rolling_the_seed_reaches_the_model_and_the_entry` |
| 47 | gui | the kernel row offers exactly the flavours the builder accepts | `test_the_kernel_menu_offers_the_kernels_the_builder_knows` |
| 48 | gui | Alt-1 to Alt-6 jump tabs, Ctrl-E shows the command, F1 the shortcuts, Escape stops a build | `test_every_tab_has_a_key_and_the_keys_jump_in_order` |
| 49 | gui | a tab key survives a notebook that disagrees: the jumper swallows Tk's error rather than killing the window | the same test's last three lines |
| 50 | gui | the file menu lists the recipes shipped next to the script, and a broken one is refused without half-loading | `test_the_examples_menu_opens_a_recipe_and_reports_a_broken_one` |
| 51 | presets | eleven presets, each with its own accent and wallpaper style, all validated in CI | the preset sweep in LoaderTests, plus CI's --batch examples/ |
| 52 | presets | --preset-info prints one preset in full, including the desktop task and the firmware default | `RecipeToolTests.test_preset_info_prints_one_preset_in_full` |
| 53 | repo | tools/style_sheet.py draws the README's contact sheet from the renderer itself, so the image cannot lie | StyleSheetTests (five tests, including the tiny-tile bounds) |
| 54 | repo | the README image went from 481 kB to 233 kB, and a test keeps it under 320 kB | `test_the_checked_in_sheet_is_small_enough_to_ship` |
| 55 | repo | CI exercises every headless flag, including --diff's expected exit 1 under set -e | .github/workflows/ci.yml, rehearsed locally as a bash -e script |
| 56 | repo | mypy --strict now covers tools/ as well as the four scripts | the files list in pyproject.toml; python3 -m mypy is clean |
| 57 | repo | test doubles are held to the real contract: a fake BuildRun owes .log, because hints read it | the stubs in CliTests, and the failure that proved the point |
| 58 | repo | README section 3 documents the flags, the tabs and the environment, not just the buttons | README.md, cross-checked against the parser and the tab builders |

| 59 | portability | the hook no longer needs GNU `sed`: the GRUB edit is two `-e` expressions written through a temp file, which BSD `sed` on macOS accepts too (CI caught the old form) | HookExecutionTests.test_the_boot_menu_timeout_is_written_and_hidden_at_zero, and the scratch-file assertion in it |
| 60 | portability | a filesystem that refuses symlinks cannot fail a build any more: `/etc/timezone` is written first and `ln` is allowed to fail, so the Windows runners pass | test_the_hook_sets_the_clock_the_locale_and_the_keymap asserts /etc/timezone everywhere and /etc/localtime wherever the shell's ln actually worked |
| 61 | testing | the generated hook is syntax-checked by `sh -n` for four feature combinations, so quoting and continuations are validated by a shell rather than by reading | test_the_hook_is_valid_shell_on_this_platform |
## What this round did not do

* **Nobody has seen the GUI running.** This environment has no display and no Tk, so the
  window is covered by tests against a fake `tkinter`, plus real-Tk tests that skip when there
  is nothing to open. Layout, fonts and HiDPI scaling stay unverified by machine.
* **No ISO was built from this branch here.** The Docker job runs on `main`. The flags it uses
  (`--build`, `--json`, the branding hook, `--tail`) are rehearsed in CI's headless step and in
  stubbed `BuildRun` tests, which is not the same thing as a bootable image.
* Two deliberate non-features: `--fix` does not rewrite files in place (it prints, and writes
  only with `--out`), and `wallpaper_seed` is not applied to the geometric styles, where a seed
  would change nothing.
* The "777 improvements" request is answered with this table rather than a padded number. A
  bigger tranche is available on request; it will still be one row per real change.
