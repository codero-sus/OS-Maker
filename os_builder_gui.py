#!/usr/bin/env python3
"""The Tk front-end for OS Builder.

All the logic lives in :mod:`os_builder`; this file only draws widgets and moves
values between them and :class:`os_builder.RecipeModel`.  That split is what makes the
app testable without a display -- the model is the application, the view is a reporter
-- and it means the terminal modes do exactly what the buttons do.

Run it with ``python3 os_builder.py``.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import tkinter as tk
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.font import nametofont
from typing import Literal

import os_builder as ob

# Deliberately dark and quiet, so the wallpaper preview is the loudest thing on screen.
BACKGROUND = "#0f1218"
FIELD = "#11151d"
PANEL = "#161b24"
PANEL_HIGH = "#1d2430"
TEXT = "#e6edf3"
TEXT_DIM = "#9aa7b8"
BORDER = "#2a3341"
OK = "#39d353"
WARN = "#f2cc60"
ERR = "#ff7b72"

UI_FAMILIES = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}
MONO_FAMILIES = {"win32": "Consolas", "darwin": "Menlo"}
UI_SIZE, MONO_SIZE = 10, 9

PREVIEW_WIDTH, PREVIEW_HEIGHT = 384, 216
SUCCESS_RE = re.compile(r"saved|written|added|created|ready|finished|loaded|copied|will copy|built|matches")
State = Literal["normal", "disabled"]

INTERACTIVE: tuple[type[tk.Misc], ...] = (
    ttk.Button,
    ttk.Combobox,
    ttk.Checkbutton,
    ttk.Radiobutton,
    ttk.Spinbox,
    tk.Button,
    tk.Entry,
    tk.Text,
    tk.Listbox,
    tk.Canvas,
)


class BuilderGUI:
    """One window: edit a recipe, watch the log, get an ISO."""

    def __init__(self, recipe: ob.Recipe | None = None, *, model: ob.RecipeModel | None = None) -> None:
        self.model = model if model is not None else ob.RecipeModel(recipe or ob.Recipe())
        self.root = tk.Tk(className=" OS Builder")
        self.root.title(f"OS Builder {ob.__version__}")
        self.root.geometry("1120x780")
        self.root.minsize(880, 640)

        self.vars: dict[str, tk.Variable | tk.Text] = {}
        self.package_buttons: dict[str, ttk.Checkbutton] = {}
        self.accent_buttons: dict[str, tk.Button] = {}
        self.preview_image: tk.PhotoImage | None = None
        self.preview_job: str | None = None
        self.poll_job: str | None = None
        self._syncing = False
        self.temp_dir = Path(self.model.branding_dir())
        self.accent = _safe_accent(self.model.recipe.accent)
        self.ui_font = (UI_FAMILIES.get(sys.platform, "DejaVu Sans"), UI_SIZE)
        self.mono_font = (MONO_FAMILIES.get(sys.platform, "DejaVu Sans Mono"), MONO_SIZE)

        self._style()
        self._build_shell()
        self._build_tabs()
        self._build_footer()
        self._build_menu()
        self._bind_keys()

        self.model.subscribe(self._on_model_change)
        self._sync_widgets()
        self._on_model_change()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self) -> None:
        """Enter the mainloop; the model outlives it only until the window closes."""
        try:
            self.root.mainloop()
        finally:
            self.model.close()

    # ------------------------------------------------------------------ theme
    def _style(self) -> None:
        style = ttk.Style(self.root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            with contextlib.suppress(tk.TclError, KeyError, ValueError):
                nametofont(name).configure(family=self.ui_font[0], size=self.ui_font[1])
        self.root.configure(background=BACKGROUND)

        style.configure(
            ".", background=BACKGROUND, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER
        )
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0, tabmargins=(12, 8, 12, 0))
        style.configure("TNotebook.Tab", padding=(16, 8), background=PANEL, foreground=TEXT_DIM)
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL_HIGH)],
            foreground=[("selected", TEXT)],
            expand=[("selected", (0, 0, 0, 2))],
        )
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=PANEL, relief="flat", borderwidth=1)
        style.configure("TLabelframe", background=BACKGROUND, foreground=TEXT_DIM, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BACKGROUND, foreground=TEXT_DIM)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("Dim.TLabel", background=BACKGROUND, foreground=TEXT_DIM)
        style.configure(
            "Title.TLabel", background=BACKGROUND, foreground=TEXT, font=(self.ui_font[0], 16, "bold")
        )
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("CardDim.TLabel", background=PANEL, foreground=TEXT_DIM)
        style.configure("TCheckbutton", background=BACKGROUND, foreground=TEXT)
        style.configure("TRadiobutton", background=BACKGROUND, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BACKGROUND)])
        style.configure("TButton", padding=(12, 6), background=PANEL_HIGH, foreground=TEXT, borderwidth=1)
        style.map(
            "TButton",
            background=[("active", "#26303f"), ("disabled", PANEL)],
            foreground=[("disabled", TEXT_DIM)],
        )
        style.configure("Go.TButton", font=(self.ui_font[0], self.ui_font[1], "bold"))
        style.configure("Horizontal.TProgressbar", background=OK, troughcolor=PANEL, bordercolor=BORDER)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=22)
        style.configure("Treeview.Heading", background=PANEL_HIGH, foreground=TEXT_DIM)
        style.map("Treeview", background=[("selected", "#2b3a52")])
        style.configure(
            "TCombobox", fieldbackground=FIELD, background=PANEL_HIGH, foreground=TEXT, arrowcolor=TEXT
        )
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)])

    # ----------------------------------------------------------------- window
    def _build_shell(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text="OS Builder", style="Title.TLabel").pack(side="left")
        self.flavour = ttk.Label(header, text="", style="Dim.TLabel")
        self.flavour.pack(side="left", padx=(14, 0))
        self.engine = ttk.Label(header, text=ob.engine_hint(), style="Dim.TLabel")
        self.engine.pack(side="right")

        self.pane = tk.PanedWindow(
            self.root,
            orient="vertical",
            sashrelief="flat",
            sashwidth=6,
            background=BACKGROUND,
            borderwidth=0,
        )
        self.pane.pack(fill="both", expand=True, padx=12, pady=(6, 0))
        self.notebook = ttk.Notebook(self.pane)
        self.pane.add(self.notebook, minsize=300, stretch="always")

        log_frame = ttk.Frame(self.pane)
        self.pane.add(log_frame, height=196, minsize=90, stretch="always")
        bar = ttk.Frame(log_frame)
        bar.pack(fill="x", pady=(2, 4))
        ttk.Label(bar, text="build log", style="Dim.TLabel").pack(side="left")
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="follow", variable=self.autoscroll).pack(side="right")
        self.log = tk.Text(
            log_frame,
            height=8,
            wrap="none",
            background="#0b0e13",
            foreground=TEXT_DIM,
            insertbackground=TEXT,
            borderwidth=1,
            relief="flat",
            highlightthickness=0,
            font=self.mono_font,
            state="disabled",
            padx=8,
            pady=6,
        )
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for tag, colour, bold in (
            ("info", TEXT_DIM, False),
            ("warn", WARN, False),
            ("err", ERR, True),
            ("ok", OK, False),
        ):
            family, size = self.mono_font
            self.log.tag_configure(
                tag, foreground=colour, font=(family, size, "bold") if bold else self.mono_font
            )

    def _build_footer(self) -> None:
        footer = ttk.Frame(self.root, padding=(16, 6, 16, 12))
        footer.pack(fill="x", side="bottom")
        self.status = ttk.Label(footer, text="", style="Dim.TLabel", wraplength=560, justify="left")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(footer, length=260, mode="determinate", maximum=1.0)
        self.progress.pack(side="left", padx=(16, 0))
        self.stop_button = ttk.Button(footer, text="stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="right")
        self.build_button = ttk.Button(
            footer, text="build ISO", style="Go.TButton", command=self._start_build
        )
        self.build_button.pack(side="right", padx=(8, 0))
        self.preview_button = ttk.Button(footer, text="dry run", command=self._dry_run)
        self.preview_button.pack(side="right", padx=(8, 0))
        self.recipe_label = ttk.Label(footer, text="", style="Dim.TLabel")
        self.recipe_label.pack(side="right", padx=(0, 16))

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="New from preset", command=self._new_recipe, accelerator="Ctrl+N")
        file_menu.add_command(label="Open recipe...", command=self._open, accelerator="Ctrl+O")
        file_menu.add_command(label="Save recipe", command=self._save, accelerator="Ctrl+S")
        file_menu.add_command(
            label="Save recipe as...", command=lambda: self._save(as_file=True), accelerator="Ctrl+Shift+S"
        )
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close, accelerator="Ctrl+Q")
        menu.add_cascade(label="file", menu=file_menu)
        tools = tk.Menu(menu, tearoff=0)
        tools.add_command(label="Generate wallpaper PNG", command=self._export_wallpaper)
        tools.add_command(label="Show the build command", command=self._show_argv)
        tools.add_command(label="Output folder", command=self._reveal_output)
        menu.add_cascade(label="tools", menu=tools)
        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="About OS Builder", command=self._about)
        help_menu.add_command(label="Keyboard shortcuts", command=self._shortcuts)
        help_menu.add_command(
            label="Project page", command=lambda: webbrowser.open("https://github.com/codero-sus/OS-Maker")
        )
        menu.add_cascade(label="help", menu=help_menu)
        self.root.config(menu=menu)

    def _bind_keys(self) -> None:
        bindings = {
            "Control-s": lambda _event: self._save(),
            "Control-S": lambda _event: self._save(as_file=True),
            "Control-o": lambda _event: self._open(),
            "Control-n": lambda _event: self._new_recipe(),
            "Control-p": lambda _event: self._dry_run(),
            "Control-b": lambda _event: self._start_build(),
            "Control-q": lambda _event: self._on_close(),
            "Escape": lambda _event: self._stop(),
        }
        for sequence, handler in bindings.items():
            self.root.bind_all(sequence, handler)

    # ------------------------------------------------------------- widget makers
    def _entry(self, row: tk.Misc, *, variable: tk.Variable, width: int) -> tk.Entry:
        """One dark, flat entry -- the theme is repeated too often to inline it."""
        entry = tk.Entry(
            row,
            textvariable=variable,
            width=width,
            background=FIELD,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=self.accent,
        )
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _card(self, parent: ttk.Frame, title: str = "") -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        card.pack(fill="x", pady=(0, 10), padx=2)
        if title:
            ttk.Label(card, text=title, style="CardDim.TLabel").pack(anchor="w", pady=(0, 6))
        return card

    def _tab(self, name: str, intro: str) -> ttk.Frame:
        frame = ttk.Frame(self.notebook, padding=(14, 12))
        self.notebook.add(frame, text=name)
        if intro:
            ttk.Label(frame, text=intro, style="Dim.TLabel", wraplength=1000, justify="left").pack(
                anchor="w", pady=(0, 10)
            )
        return frame

    def _field(self, parent: ttk.Frame, key: str, label: str, *, width: int = 46, hint: str = "") -> tk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="CardDim.TLabel", width=12, anchor="w").pack(side="left")
        var = tk.StringVar(value=str(getattr(self.model.recipe, key, "")))
        entry = self._entry(row, variable=var, width=width)
        self.vars[key] = var
        var.trace_add("write", lambda *_args: self._push(key, var))
        if hint:
            ttk.Label(parent, text=hint, style="CardDim.TLabel", wraplength=500, justify="left").pack(
                anchor="w", pady=(0, 2)
            )
        return entry

    def _choice(
        self, parent: tk.Misc, key: str, label: str, values: Sequence[str], *, label_width: int = 0
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        if label_width:
            ttk.Label(row, text=label, style="CardDim.TLabel", width=label_width, anchor="w").pack(
                side="left"
            )
        else:
            ttk.Label(row, text=label, style="CardDim.TLabel").pack(side="left", padx=(0, 6))
        var = tk.StringVar(value=str(getattr(self.model.recipe, key)))
        ttk.Combobox(row, textvariable=var, values=list(values), state="readonly", width=16).pack(side="left")
        self.vars[key] = var
        var.trace_add("write", lambda *_args: self._push(key, var))

    def _check(self, parent: tk.Misc, key: str, label: str, hint: str = "") -> ttk.Checkbutton:
        var = tk.BooleanVar(value=bool(getattr(self.model.recipe, key)))
        box = ttk.Checkbutton(
            parent, text=label, variable=var, command=lambda: self.model.set(**{key: var.get()})
        )
        box.pack(anchor="w", pady=(6, 0))
        self.vars[key] = var
        if hint:
            ttk.Label(parent, text=hint, style="CardDim.TLabel", wraplength=500, justify="left").pack(
                anchor="w"
            )
        return box

    def _multiline(self, parent: ttk.Frame, key: str, label: str, *, lines: int = 5) -> tk.Text:
        ttk.Label(parent, text=label, style="CardDim.TLabel", anchor="w").pack(anchor="w", pady=(8, 1))
        box = tk.Text(
            parent,
            height=lines,
            wrap="word",
            background=FIELD,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=self.accent,
            font=self.mono_font,
            padx=6,
            pady=5,
        )
        box.pack(fill="both", expand=True)
        self.vars[key] = box
        box.bind("<KeyRelease>", lambda _event: self._push_text(key, box))
        return box

    def _listbox(self, parent: ttk.Frame, *, height: int) -> tk.Listbox:
        return tk.Listbox(
            parent,
            height=height,
            background=FIELD,
            foreground=TEXT,
            selectbackground=self.accent,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            activestyle="none",
            font=self.mono_font,
            exportselection=False,
        )

    # ------------------------------------------------------------------- tabs
    def _build_tabs(self) -> None:
        self._identity_tab()
        self._software_tab()
        self._appearance_tab()
        self._files_tab()
        self._build_tab()

    def _identity_tab(self) -> None:
        tab = self._tab("identity", "What the machine is called, and what it says when it wakes up.")
        holder = ttk.Frame(tab)
        holder.pack(fill="x")
        left = self._card(holder, "naming")
        self._field(
            left,
            "name",
            "product name",
            hint="Shown on the wallpaper, in /etc/os-release and in the ISO's file name.",
        )
        self._field(
            left, "tagline", "tagline", hint="One line under the name, on the wallpaper and the login banner."
        )
        self._field(left, "hostname", "hostname", hint="Blank means: derived from the product name.")
        self._field(left, "notes", "notes", hint="Stored in the recipe only, for whoever reads it next.")
        right = self._card(holder, "message of the day")
        self._multiline(right, "motd", "shown at login (blank gets the generated one)")

        base = self._card(tab, "the debian underneath")
        grid = ttk.Frame(base)
        grid.pack(fill="x")
        for index, (key, label, values) in enumerate(
            (
                ("release", "release", ob.RELEASES),
                ("architecture", "architecture", ob.ARCHITECTURES),
                ("compression", "compression", ob.COMPRESSIONS),
                ("container", "container", ob.CONTAINERS),
            )
        ):
            cell = ttk.Frame(grid)
            cell.grid(row=0, column=index, sticky="w", padx=(0, 22))
            self._choice(cell, key, label, values)

    def _software_tab(self) -> None:
        tab = self._tab(
            "software",
            "Pick a starting point, then tick what you want. Names are Debian package names and are "
            "passed straight to the builder.",
        )
        top = self._card(tab, "starting point")
        row = ttk.Frame(top)
        row.pack(fill="x")
        var = tk.StringVar(value=self.model.recipe.preset)
        ttk.Combobox(row, textvariable=var, values=list(ob.PRESETS), state="readonly", width=14).pack(
            side="left"
        )
        ttk.Button(row, text="apply preset", command=lambda: self.model.set_preset(var.get())).pack(
            side="left", padx=(10, 0)
        )
        self.vars["preset"] = var
        self.preset_blurb = ttk.Label(top, text="", style="CardDim.TLabel", wraplength=720, justify="left")
        self.preset_blurb.pack(anchor="w", pady=(6, 0))

        chosen = self._card(tab, "in this image")
        self.package_list = tk.Listbox(
            chosen,
            height=6,
            background=FIELD,
            foreground=TEXT,
            selectbackground=self.accent,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            activestyle="none",
            font=self.mono_font,
            exportselection=False,
        )
        self.package_list.pack(fill="x")
        add_row = ttk.Frame(chosen)
        add_row.pack(fill="x", pady=(8, 0))
        self.add_package = tk.StringVar()
        entry = self._entry(add_row, variable=self.add_package, width=32)
        entry.bind("<Return>", lambda _event: self._add_packages())
        ttk.Button(add_row, text="add", command=self._add_packages).pack(side="left", padx=(8, 0))
        ttk.Button(add_row, text="remove selected", command=self._remove_package).pack(
            side="left", padx=(8, 0)
        )

        grid = self._card(tab, "or tick what you need")
        holder = ttk.Frame(grid)
        holder.pack(fill="x")
        for index, name in enumerate(self.model.suggestions):
            state = tk.BooleanVar(value=name in self.model.recipe.packages)
            button = ttk.Checkbutton(
                holder,
                text=name,
                variable=state,
                command=self._package_toggle(name, state),
            )
            button.grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 18), pady=2)
            self.package_buttons[name] = button
        self._check(
            grid,
            "with_firmware",
            "include firmware (contrib / non-free)",
            hint="Needed by some Wi-Fi and GPU drivers that live outside the main archive.",
        )

    def _appearance_tab(self) -> None:
        tab = self._tab(
            "appearance", "The generated wallpaper, login banner and branding. The preview is live."
        )
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        left = ttk.Frame(holder)
        left.pack(side="left", fill="y")
        right = ttk.Frame(holder)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        swatches = self._card(left, "accent colour")
        strip = ttk.Frame(swatches)
        strip.pack(anchor="w")
        for index, colour in enumerate(ob.ACCENTS):
            button = tk.Button(
                strip,
                background=colour,
                activebackground=colour,
                relief="flat",
                width=4,
                bd=0,
                highlightthickness=1,
                highlightbackground=BORDER,
                command=self._accent_pick(colour),
            )
            button.grid(row=0, column=index, padx=2, ipady=8)
            self.accent_buttons[colour] = button
        self._field(
            swatches,
            "accent",
            "hex",
            width=12,
            hint="Drives the wallpaper, this window's button, and /etc/os-release.",
        )

        styles = self._card(left, "wallpaper")
        style_var = tk.StringVar(value=self.model.recipe.wallpaper_style)
        for name in ob.WALLPAPER_STYLES:
            ttk.Radiobutton(
                styles,
                text=name,
                value=name,
                variable=style_var,
                command=lambda: self.model.set(wallpaper_style=style_var.get()),
            ).pack(anchor="w", pady=1)
        self.vars["wallpaper_style"] = style_var
        self._field(
            styles,
            "wallpaper_size",
            "size",
            width=14,
            hint="1920x1080 is a good default; bigger costs a few seconds of render.",
        )
        self._check(
            styles,
            "with_wallpaper",
            "stamp the name onto the wallpaper",
            hint="Off gives you a clean, textless image.",
        )

        preview_card = self._card(right, "preview")
        self.canvas = tk.Canvas(
            preview_card,
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
            background="#0b0e13",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        buttons = ttk.Frame(preview_card)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="save wallpaper PNG...", command=self._export_wallpaper).pack(side="left")
        self.preview_note = ttk.Label(buttons, text="", style="CardDim.TLabel")
        self.preview_note.pack(side="right")

    def _files_tab(self) -> None:
        tab = self._tab(
            "files & hooks",
            "Files land at the path you choose inside the image. Hooks are shell scripts live-build runs while "
            "building, so they must be short, idempotent and never wait for input.",
        )
        files = self._card(tab, "copy into the image")
        self.file_list = self._listbox(files, height=7)
        self.file_list.pack(fill="x")
        row = ttk.Frame(files)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="add file...", command=self._add_file).pack(side="left")
        ttk.Button(row, text="remove selected", command=self._remove_file).pack(side="left", padx=(8, 0))
        ttk.Label(
            row,
            text="entries look like  /home/you/keys.pub:/root/.ssh/authorized_keys",
            style="CardDim.TLabel",
        ).pack(side="left", padx=(12, 0))

        hooks = self._card(tab, "run while building")
        self.hook_list = self._listbox(hooks, height=5)
        self.hook_list.pack(fill="x")
        row = ttk.Frame(hooks)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="add hook...", command=self._add_hook).pack(side="left")
        ttk.Button(row, text="remove selected", command=self._remove_hook).pack(side="left", padx=(8, 0))
        ttk.Label(hooks, text="The branding hook is always added for you.", style="CardDim.TLabel").pack(
            anchor="w", pady=(6, 0)
        )

    def _build_tab(self) -> None:
        tab = self._tab("build", "Where the output goes, and how hard the builder should try.")
        paths = self._card(tab, "folders")
        self._field(paths, "output_dir", "output", hint="Blank means dist/ next to the scripts.")
        self._field(
            paths,
            "work_dir",
            "work dir",
            hint="Blank means a temporary directory that is removed afterwards.",
        )
        self._field(paths, "cache_dir", "cache", hint="A package cache makes repeat builds much faster.")
        row = ttk.Frame(paths)
        row.pack(anchor="w", pady=(6, 0))
        ttk.Button(row, text="choose output folder...", command=self._choose_output).pack(side="left")
        ttk.Button(row, text="open output folder", command=self._reveal_output).pack(side="left", padx=(8, 0))

        options = self._card(tab, "options")
        grid = ttk.Frame(options)
        grid.pack(anchor="w")
        self._check(grid, "boot_test", "boot the ISO in QEMU afterwards")
        self._check(
            grid,
            "keep_work",
            "keep the build workspace",
            hint="Bigger, but you can inspect exactly what went in.",
        )
        self._check(
            grid,
            "skip_checks",
            "skip the preflight checks",
            hint="Only if disk space and tooling are known-good.",
        )

        found = self._card(tab, "what this produced")
        self.artifacts = ttk.Treeview(
            found, columns=("kind", "size", "age"), height=6, show="tree headings", selectmode="browse"
        )
        self.artifacts.heading("#0", text="file")
        self.artifacts.column("#0", width=430, anchor="w")
        for key, label, width in (("kind", "kind", 100), ("size", "size", 90), ("age", "age", 100)):
            self.artifacts.heading(key, text=label)
            self.artifacts.column(key, width=width, anchor="w")
        self.artifacts.pack(fill="both", expand=True)
        bar = ttk.Frame(found)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="verify checksum", command=self._verify).pack(side="left")
        ttk.Button(bar, text="open the log", command=self._open_log).pack(side="left", padx=(8, 0))
        self.build_note = ttk.Label(bar, text="", style="CardDim.TLabel")
        self.build_note.pack(side="right")

    # ---------------------------------------------------------- model <-> view
    def _package_toggle(self, name: str, state: tk.BooleanVar) -> Callable[[], None]:
        """A bound handler, so the loop variable is captured per package and not per click."""

        def apply() -> None:
            self.model.toggle_package(name, on=state.get())

        return apply

    def _accent_pick(self, colour: str) -> Callable[[], None]:
        def apply() -> None:
            self.model.set(accent=colour)

        return apply

    def _push(self, key: str, var: tk.Variable) -> None:
        """Widget -> model, via a trace.  Suppressed while the view is being synced."""
        if self._syncing:
            return
        value = var.get()
        if key == "motd" and isinstance(value, str):
            value = value.rstrip("\n") + "\n" if value.strip() else ""
        self.model.set(**{key: value})

    def _push_text(self, key: str, box: tk.Text) -> None:
        if self._syncing:
            return
        self.model.set(**{key: box.get("1.0", "end-1c")})

    def _sync_widgets(self) -> None:
        """Model -> widgets, when a recipe is loaded or a preset applied."""
        self._syncing = True
        try:
            for key, var in self.vars.items():
                value = getattr(self.model.recipe, key, None)
                if value is None:
                    continue
                if isinstance(var, tk.Text):
                    var.delete("1.0", "end")
                    var.insert("1.0", str(value))
                elif isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    var.set(str(value))
        finally:
            self._syncing = False

    def _on_model_change(self) -> None:
        recipe = self.model.recipe
        name = recipe.name.strip() or "untitled"
        self.flavour.configure(text=f"{name} - {recipe.release} - {recipe.architecture}")
        self._refresh_packages()
        self._refresh_files()
        self._apply_accent(recipe.accent)
        self._refresh_status()
        self._queue_preview()

    def _refresh_packages(self) -> None:
        chosen = set(self.model.recipe.packages)
        for name, button in self.package_buttons.items():
            variable = button.cget("variable")
            if isinstance(variable, tk.BooleanVar) and variable.get() != (name in chosen):
                variable.set(name in chosen)
        self.package_list.delete(0, "end")
        for name in self.model.recipe.merged_packages():
            self.package_list.insert("end", name)
        preset = ob.PRESETS.get(self.model.recipe.preset)
        self.preset_blurb.configure(text=f"{preset.label}: {preset.blurb}" if preset else "")

    def _refresh_files(self) -> None:
        for box, entries in (
            (self.file_list, self.model.recipe.files),
            (self.hook_list, self.model.recipe.hooks),
        ):
            box.delete(0, "end")
            for entry in entries:
                box.insert("end", entry)

    def _apply_accent(self, accent: str) -> None:
        """The window adopts the accent colour of the OS being built."""
        if accent == self.accent or not ob.HEX_COLOR_RE.match(accent):
            return
        self.accent = accent
        style = ttk.Style(self.root)
        style.configure("Horizontal.TProgressbar", background=accent)
        style.configure("Go.TButton", background=accent, foreground="#0b0e13")
        red, green, blue = ob.hex_to_rgb(accent)
        bright = "#{:02x}{:02x}{:02x}".format(*(min(255, value + 34) for value in (red, green, blue)))
        style.map("Go.TButton", background=[("active", bright)])
        for colour, button in self.accent_buttons.items():
            button.configure(
                highlightbackground=TEXT if colour == accent else BORDER, bd=2 if colour == accent else 0
            )
        for box in self.package_list, self.file_list, self.hook_list:
            box.configure(selectbackground=accent)

    def _refresh_status(self) -> None:
        run = self.model.run
        problems = self.model.problems()
        if run is not None and run.running:
            self.status.configure(text="building - see the log below", foreground=TEXT)
        elif problems:
            self.status.configure(text=f"{len(problems)} to fix: {problems[0]}", foreground=WARN)
        else:
            count = len(self.model.recipe.merged_packages())
            self.status.configure(text=f"ready - {count} packages - {ob.engine_hint()}", foreground=OK)
        if run is None or not run.running:
            self.build_button.configure(state="normal" if not problems else "disabled")
        path = self.model.path
        self.recipe_label.configure(text=f"recipe: {path.name}" if path else "recipe: unsaved")

    def _log(self, line: str, *, tag: str | None = None) -> None:
        """Append one line to the log panel, coloured by what it says."""
        clean = ob.strip_ansi(line).rstrip()
        if tag is None:
            # Warnings first: the builder writes "os-maker: warning: ...", which the error
            # pattern would otherwise claim.
            if ob.WARNING_RE.search(clean):
                tag = "warn"
            elif ob.ERROR_RE.search(clean):
                tag = "err"
            elif SUCCESS_RE.search(clean):
                tag = "ok"
            else:
                tag = "info"
        self.log.configure(state="normal")
        self.log.insert("end", clean + "\n", tag)
        if self.autoscroll.get():
            self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- preview
    def _queue_preview(self, delay: int = 260) -> None:
        """Coalesce preview renders: typing a name should not redraw 20 times."""
        if self.preview_job is not None:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(delay, self._render_preview)

    def _render_preview(self) -> None:
        self.preview_job = None
        try:
            path = self.model.preview_image(self.temp_dir, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT)
            image = tk.PhotoImage(file=str(path))
        except (tk.TclError, ob.RecipeError) as exc:
            self.preview_note.configure(text="preview unavailable")
            self._log(f"os-builder: the preview could not be drawn ({exc})")
            return
        self.preview_image = image
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=image)
        width, height = ob.read_png_size(path)
        self.preview_note.configure(
            text=f"preview {width}x{height} - shipped at {self.model.recipe.wallpaper_size}"
        )

    def _export_wallpaper(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save the wallpaper",
            initialfile=f"{self.model.recipe.resolved_hostname}-wallpaper.png",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
        )
        if not path:
            return
        size = ob.parse_size(self.model.recipe.wallpaper_size) or (1920, 1080)
        try:
            ob.render_wallpaper_png(self.model.recipe, Path(path), width=size[0], height=size[1])
        except ob.RecipeError as exc:
            messagebox.showerror("OS Builder", str(exc))
            return
        self._log(f"os-builder: wallpaper written to {path}")
        messagebox.showinfo("OS Builder", f"Wallpaper written to\n{path}")

    # -------------------------------------------------------------- building
    def _show_argv(self) -> None:
        text = ob.describe_argv(self.model.argv())
        self._log(f"os-builder: {text}")
        window = tk.Toplevel(self.root)
        window.title("build command")
        window.geometry("760x150")
        window.configure(background=BACKGROUND)
        box = tk.Text(
            window, height=5, wrap="word", background=PANEL, foreground=TEXT, insertbackground=TEXT, bd=0
        )
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(window, text="copy to clipboard", command=lambda: self._copy(text)).pack(pady=(0, 8))

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("os-builder: copied the build command to the clipboard")

    def _dry_run(self) -> None:
        run = self.model.run
        if run is not None and run.running:
            messagebox.showinfo("OS Builder", "A build is running - wait for it or stop it first.")
            return
        self.preview_button.configure(state="disabled")
        self._log("os-builder: asking the builder for a plan...")
        code, text = self.model.preview()
        for line in text.splitlines():
            self._log(line)
        self._log(f"os-builder: the dry run exited {code}", tag="ok" if code == 0 else "err")
        self.progress.configure(value=0.0)
        self.preview_button.configure(state="normal")

    def _start_build(self) -> None:
        run = self.model.run
        if run is not None and run.running:
            return
        problems = self.model.problems()
        if problems:
            messagebox.showerror(
                "OS Builder", "Fix this first:\n\n" + "\n".join(f"- {item}" for item in problems)
            )
            return
        try:
            run = self.model.start_build()
        except ob.RecipeError as exc:
            messagebox.showerror("OS Builder", str(exc))
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._log(f"os-builder: starting: {ob.describe_argv(run.argv)}", tag="ok")
        self._set_running(True)
        self.progress.configure(value=0.0, mode="determinate")
        self._poll()

    def _set_running(self, running: bool) -> None:
        """Lock the form while a build runs, so the recipe cannot change mid-image."""
        state: State = "disabled" if running else "normal"
        self.stop_button.configure(state="normal" if running else "disabled")
        self.build_button.configure(state=state)
        self.preview_button.configure(state=state)
        for child in self.notebook.winfo_children():
            _set_state_tree(child, state)

    def _poll(self) -> None:
        run = self.model.run
        if run is None:
            return
        for line in run.drain():
            self._log(line)
        progress = run.progress
        self.progress.configure(value=min(1.0, progress.fraction))
        if run.running:
            self.status.configure(text=f"{progress.text}  ({run.elapsed:.0f}s)")
            self.poll_job = self.root.after(120, self._poll)
            return
        self.poll_job = None
        self._set_running(False)
        self.status.configure(
            text=f"{progress.text} in {run.elapsed:.0f}s", foreground=OK if run.finish_code == 0 else ERR
        )
        artifacts = self.model.artifacts()
        self._refresh_artifacts(artifacts)
        iso = next((item for item in artifacts if item.path.suffix == ".iso"), None)
        if run.finish_code == 0 and iso is not None:
            self._log(f"os-builder: built {iso.path} ({iso.human_size})", tag="ok")
            if messagebox.askyesno(
                "OS Builder", f"{iso.path.name} is ready ({iso.human_size}).\n\nOpen the folder?"
            ):
                self._reveal(iso.path.parent)
        elif run.finish_code == 0:
            self._log(
                "os-builder: the builder exited cleanly but left no ISO in the output folder", tag="warn"
            )
        else:
            self._log(f"os-builder: build stopped with exit {run.finish_code}", tag="err")

    def _refresh_artifacts(self, artifacts: Sequence[ob.Artifact] | None = None) -> None:
        items = list(self.model.artifacts()) if artifacts is None else list(artifacts)
        self.artifacts.delete(*self.artifacts.get_children())
        for artifact in items:
            self.artifacts.insert(
                "", "end", text=artifact.path.name, values=(artifact.kind, artifact.human_size, artifact.age)
            )
        self.build_note.configure(
            text=f"{len(items)} file(s) in {self.model.recipe.resolved_output_dir}"
            if items
            else "nothing built yet"
        )

    def _stop(self) -> None:
        run = self.model.run
        if run is not None and run.running:
            run.stop()
            self._log("os-builder: asked the builder to stop", tag="warn")

    def _verify(self) -> None:
        rows = self.artifacts.selection() or self.artifacts.get_children()
        if not rows:
            messagebox.showinfo("OS Builder", "Nothing to verify yet - build something first.")
            return
        output = self.model.recipe.resolved_output_dir
        iso = output / self.artifacts.item(rows[0], "text")
        if iso.suffix != ".iso":
            found = sorted(output.glob("*.iso"))
            if not found:
                messagebox.showinfo("OS Builder", "There is no ISO in the output folder to check.")
                return
            iso = found[0]
        ok, message = ob.verify_checksum(iso)
        self._log(f"os-builder: {message}", tag="ok" if ok else "warn")
        if ok:
            messagebox.showinfo("OS Builder", message)
        else:
            messagebox.showwarning("OS Builder", message)

    def _open_log(self) -> None:
        output = self.model.recipe.resolved_output_dir
        logs = sorted(output.glob("*.log"), key=lambda path: path.stat().st_mtime) if output.is_dir() else []
        if not logs:
            messagebox.showinfo("OS Builder", "No build log in the output folder yet.")
            return
        window = tk.Toplevel(self.root)
        window.title(logs[-1].name)
        window.geometry("920x620")
        window.configure(background=BACKGROUND)
        box = tk.Text(
            window,
            wrap="none",
            background="#0b0e13",
            foreground=TEXT,
            insertbackground=TEXT,
            bd=0,
            font=self.mono_font,
        )
        box.insert("1.0", logs[-1].read_text(encoding="utf-8", errors="replace"))
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)

    # ----------------------------------------------------------- file pickers
    def _add_file(self) -> None:
        source = filedialog.askopenfilename(title="Add a file to the image")
        if not source:
            return
        destination = _prompt(
            self.root, "Where in the image?", Path(source).name, "/root/" + Path(source).name
        )
        if destination:
            self._log(f"os-builder: will copy {self.model.add_file(source, destination)}")

    def _remove_file(self) -> None:
        selected = self.file_list.curselection()
        if selected:
            self.model.remove_file(selected[0])

    def _add_hook(self) -> None:
        path = filedialog.askopenfilename(
            title="Add a build hook", filetypes=[("shell script", "*.sh"), ("all files", "*.*")]
        )
        if path:
            self.model.add_hook(path)
            self._log(f"os-builder: hook added: {path}")

    def _remove_hook(self) -> None:
        selected = self.hook_list.curselection()
        if selected:
            self.model.remove_hook(selected[0])

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Where should the ISO go?")
        if path:
            self.model.set(output_dir=path)

    def _reveal_output(self) -> None:
        directory = self.model.recipe.resolved_output_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"os-builder: cannot create {directory} ({exc})", tag="warn")
            return
        self._reveal(directory)

    def _reveal(self, directory: Path) -> None:
        opener = {"win32": "explorer", "darwin": "open"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([opener, str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._log(f"os-builder: could not open {directory} ({exc})", tag="warn")

    # -------------------------------------------------------------- packages
    def _add_packages(self) -> None:
        added = self.model.add_packages(self.add_package.get())
        self.add_package.set("")
        if added:
            self._log(f"os-builder: added {', '.join(added)}")
        elif self.add_package.get().strip():
            self._log("os-builder: that package is already in the image", tag="warn")

    def _remove_package(self) -> None:
        selected = self.package_list.curselection()
        if not selected:
            return
        name = self.package_list.get(selected[0])
        preset = ob.PRESETS.get(self.model.recipe.preset)
        if preset is not None and name in preset.packages:
            messagebox.showinfo(
                "OS Builder",
                f"{name} comes from the '{preset.key}' preset - choose another preset (or 'minimal') to drop it.",
            )
            return
        self.model.toggle_package(name, on=False)

    # --------------------------------------------------------------- recipes
    def _save(self, *, as_file: bool = False) -> None:
        path = self.model.path
        if as_file or path is None:
            chosen = filedialog.asksaveasfilename(
                title="Save the recipe",
                defaultextension=".json",
                initialfile=f"{self.model.recipe.resolved_hostname}.json",
                filetypes=[("OS recipe", "*.json"), ("all files", "*.*")],
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            self.model.save(path)
        except ob.RecipeError as exc:
            messagebox.showerror("OS Builder", str(exc))
            return
        self._log(f"os-builder: recipe saved to {path}")
        self._refresh_status()

    def _open(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open a recipe", filetypes=[("OS recipe", "*.json"), ("all files", "*.*")]
        )
        if chosen:
            self._load(Path(chosen))

    def _load(self, path: Path) -> None:
        try:
            self.model.open(path)
        except ob.RecipeError as exc:
            messagebox.showerror("OS Builder", str(exc))
            return
        self._sync_widgets()
        self._on_model_change()
        self._log(f"os-builder: loaded {path.name}")

    def _new_recipe(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("new recipe from a preset")
        window.configure(background=BACKGROUND)
        window.resizable(False, False)
        ttk.Label(window, text="start from", style="Dim.TLabel").pack(anchor="w", padx=12, pady=(12, 2))
        var = tk.StringVar(value=self.model.recipe.preset)
        ttk.Combobox(window, textvariable=var, values=list(ob.PRESETS), state="readonly", width=16).pack(
            anchor="w", padx=12
        )
        keep = tk.BooleanVar(value=True)
        ttk.Checkbutton(window, text="keep my colours", variable=keep).pack(anchor="w", padx=12, pady=(8, 0))
        actions = ttk.Frame(window)
        actions.pack(fill="x", padx=12, pady=12)

        def apply() -> None:
            try:
                recipe = ob.Recipe.for_preset(var.get())
            except ob.RecipeError as exc:
                messagebox.showerror("OS Builder", str(exc))
                return
            if keep.get():
                recipe.accent = self.model.recipe.accent
                recipe.wallpaper_style = self.model.recipe.wallpaper_style
            self.model.recipe = recipe
            self.model.path = None
            self._sync_widgets()
            self._on_model_change()
            window.destroy()

        ttk.Button(actions, text="start here", command=apply).pack(side="right")
        ttk.Button(actions, text="cancel", command=window.destroy).pack(side="right", padx=(0, 8))

    # ------------------------------------------------------------------ about
    def _about(self) -> None:
        messagebox.showinfo(
            "about OS Builder",
            f"OS Builder {ob.__version__}\n\n"
            "A recipe-driven front-end for build_os.py: choose the look, the packages and the files, "
            "and it generates the artwork and runs the real live-build pipeline for you.\n\n"
            "Standard library only - the wallpaper you are looking at is a PNG written by hand with zlib.",
        )

    def _shortcuts(self) -> None:
        rows = (
            ("Ctrl+S", "save the recipe"),
            ("Ctrl+Shift+S", "save the recipe somewhere else"),
            ("Ctrl+O", "open a recipe"),
            ("Ctrl+N", "new recipe from a preset"),
            ("Ctrl+P", "dry run (plan, no build)"),
            ("Ctrl+B", "build the ISO"),
            ("Esc", "stop a running build"),
            ("Ctrl+Q", "quit"),
        )
        messagebox.showinfo("keyboard", "\n".join(f"{keys:<16}{what}" for keys, what in rows))

    def _on_close(self) -> None:
        run = self.model.run
        if run is not None and run.running:
            if not messagebox.askyesno("OS Builder", "A build is still running. Stop it and quit?"):
                return
            run.stop()
        if self.poll_job is not None:
            self.root.after_cancel(self.poll_job)
        self.model.close()
        self.root.destroy()


def _set_state_tree(widget: tk.Misc, state: State) -> None:
    """Give every interactive descendant the same state (used while a build runs)."""
    for child in widget.winfo_children():
        if isinstance(child, INTERACTIVE):
            # The dict form is deliberate: Tk types `state=` per widget class, and this
            # loop does not know which class it will meet next.
            with contextlib.suppress(tk.TclError):
                child.configure({"state": state})
        _set_state_tree(child, state)


def _prompt(parent: tk.Tk, title: str, detail: str, default: str) -> str:
    """A themed one-line prompt, because simpledialog ignores our colours."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.configure(background=BACKGROUND)
    window.transient(parent)
    window.grab_set()
    ttk.Label(window, text=detail, style="Dim.TLabel", wraplength=420, justify="left").pack(
        anchor="w", padx=12, pady=(12, 2)
    )
    var = tk.StringVar(value=default)
    entry = tk.Entry(
        window,
        textvariable=var,
        width=64,
        background=FIELD,
        foreground=TEXT,
        insertbackground=TEXT,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER,
        font=("TkFixedFont",),
    )
    entry.pack(fill="x", padx=12, pady=(0, 8))
    entry.icursor("end")
    result = {"value": ""}
    buttons = ttk.Frame(window)
    buttons.pack(fill="x", padx=12, pady=(0, 12))

    def okay() -> None:
        result["value"] = var.get().strip()
        window.destroy()

    ttk.Button(buttons, text="ok", command=okay).pack(side="right")
    ttk.Button(buttons, text="cancel", command=window.destroy).pack(side="right", padx=(0, 8))
    entry.bind("<Return>", lambda _event: okay())
    entry.bind("<Escape>", lambda _event: window.destroy())
    window.wait_window()
    return result["value"]


def _safe_accent(accent: str) -> str:
    """The recipe's accent if it is usable, otherwise the default swatch."""
    return accent if ob.HEX_COLOR_RE.match(accent) else ob.ACCENTS[0]
