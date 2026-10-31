#!/usr/bin/env python3
"""
B.O.L.T. - Budget Optogenetic Light Tool
Control GUI for the BOLT Arduino firmware (bolt_firmware.ino)

Installed via pip (see pyproject.toml):  bolt-gui
Run directly without installing:         python bolt_gui.py
"""

import json
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports
import threading
import time

NUM_CHANNELS = 4
PWM_PINS = [3, 5, 6, 9, 10, 11]          # PWM-capable digital pins (Arduino Uno)
DEFAULT_PINS = [3, 9, 10, 11]

RECENT_PATH = os.path.join(os.path.expanduser("~"), ".bolt_recent_protocols.json")
RECENT_MAX = 5

# USB vendor IDs seen on Arduino Uno and common Uno-compatible boards
# (e.g. Elegoo): 0x2341 official Arduino, 0x1A86 CH340 clone, 0x10C4 CP210x
# clone, 0x0403 FTDI clone.
ARDUINO_VIDS = {0x2341, 0x1A86, 0x10C4, 0x0403}

NO_PORT_LABEL = "No available port"

# ---------------------------------------------------------- PULSED TIMING
# DEFAULT_ON_MS is just the starting value for a new channel's On (ms) box
# -- on-time is freely editable per channel, not a hardware constant.
DEFAULT_ON_MS = 250

# ---------------------------------------------------------------- PALETTES
PALETTES = {
    "light": {
        "BG_DARK":     "#eef1f7",   # main window background
        "BG_PANEL":    "#ffffff",   # panel / labelframe background
        "BG_CANVAS":   "#f4f6fb",   # LED preview background
        "FG_TEXT":     "#1c2333",
        "FG_MUTED":    "#6b7386",
        "ACCENT":      "#2f5fe0",
        "ACCENT_DIM":  "#dce6ff",
        "STRIP_OFF":   "#c7cede",
        "OK_GREEN":    "#1f9d55",
        "ERR_RED":     "#d1364a",
        "BORDER":      "#d5dae5",
        "BORDER_SOFT": "#e6e9f0",
        "ENTRY_BG":    "#ffffff",
        "BTN_BG":      "#e7ebf3",
    },
    "dark": {
        "BG_DARK":     "#0d1117",
        "BG_PANEL":    "#161b22",
        "BG_CANVAS":   "#0d1117",
        "FG_TEXT":     "#e6edf3",
        "FG_MUTED":    "#8b949e",
        "ACCENT":      "#4d8eff",
        "ACCENT_DIM":  "#1c3a63",
        "STRIP_OFF":   "#30363d",
        "OK_GREEN":    "#3fb950",
        "ERR_RED":     "#f85149",
        "BORDER":      "#30363d",
        "BORDER_SOFT": "#21262d",
        "ENTRY_BG":    "#0d1117",
        "BTN_BG":      "#21262d",
    },
}

FONT_FAMILY   = "Segoe UI"
FONT_BASE     = (FONT_FAMILY, 10)
FONT_BASE_B   = (FONT_FAMILY, 10, "bold")
FONT_LOGO     = (FONT_FAMILY, 15, "bold")
FONT_MONO     = ("Consolas", 9)


class Channel:
    """Holds one LED channel's settings.

    A channel is either flashing on and off ("pulsed") or lit all the
    time ("continuous") -- never both. Brightness (flash_intensity) and
    timing are separate: brightness is how strong the light is while lit;
    timing is how long it's on and off for each flash.

    Duty cycle (%) and the On/Off times are two views of the same thing --
    changing one updates the other automatically (see apply_duty_pct and
    sync_duty_from_on_off), so they can never disagree.

    Ticking Continuous (force_continuous) just hides the pulsing without
    losing it -- untick it and the previous On/Off pattern comes back."""

    def __init__(self, index):
        self.index = index
        self.enabled  = tk.BooleanVar(value=False)
        self.pulsed_intensity = tk.DoubleVar(value=50.0)            # 0.5-100 %, derived view
        self.on_time_ms  = tk.DoubleVar(value=float(DEFAULT_ON_MS))
        self.off_time_ms = tk.DoubleVar(value=float(DEFAULT_ON_MS))  # 50% duty by default
        self.time_unit = tk.StringVar(value="ms")                    # display unit for On/Off boxes
        self.force_continuous = tk.BooleanVar(value=False)           # explicit 100%-continuous override
        self.on_time_display  = tk.StringVar(value="")               # formatted On box text
        self.off_time_display = tk.StringVar(value="")               # formatted Off box text
        self.flash_intensity = tk.StringVar(value="255")             # 1-255 PWM level, typed
        self.duration = tk.DoubleVar(value=0.0)                       # minutes
        self.pin      = tk.StringVar(value=str(DEFAULT_PINS[index]))
        self.refresh_time_display()

    def get_pulse_timing(self):
        """Returns (on time, off time, is continuous?) in milliseconds.
        Continuous whenever the Continuous checkbox is ticked."""
        on_ms = max(1.0, self.on_time_ms.get())
        off_ms = max(0.0, self.off_time_ms.get())
        if self.force_continuous.get():
            return on_ms, 0.0, True
        return on_ms, off_ms, False

    def get_duty_pct(self):
        on_ms, off_ms, continuous = self.get_pulse_timing()
        if continuous:
            return 100.0
        return max(0.5, min(100.0, on_ms / (on_ms + off_ms) * 100.0))

    def apply_duty_pct(self, target_pct):
        """Called when the duty cycle (%) box is changed: works out the
        new Off time needed to hit that percentage, keeping On the same."""
        on_ms = max(1.0, self.on_time_ms.get())
        target_pct = max(0.5, min(100.0, target_pct))
        new_off = 0.0 if target_pct >= 100.0 else on_ms * (100.0 - target_pct) / target_pct
        self.off_time_ms.set(round(new_off, 1))
        self.pulsed_intensity.set(target_pct)

    def sync_duty_from_on_off(self):
        """Called when On or Off is typed in directly: updates the duty
        cycle (%) box to match, so it never shows a stale value."""
        self.pulsed_intensity.set(round(self.get_duty_pct(), 1))

    def refresh_time_display(self):
        """Updates what the On/Off boxes actually show, in the chosen
        unit (ms or s). Shows "infinity" for On, and "N/A" for Off, when
        the channel is continuous -- there's no on/off pattern to show."""
        _on_ms, _dark_ms, continuous = self.get_pulse_timing()
        unit = self.time_unit.get()
        if continuous:
            self.on_time_display.set("∞")
        else:
            v = self.on_time_ms.get()
            self.on_time_display.set(f"{(v / 1000.0 if unit == 's' else v):g}")
        if self.force_continuous.get():
            self.off_time_display.set("N/A")
        else:
            v = self.off_time_ms.get()
            self.off_time_display.set(f"{(v / 1000.0 if unit == 's' else v):g}")

    def parse_time_input(self, text, fallback_ms):
        """Turns a typed On/Off value into milliseconds. If what was
        typed isn't a valid number, keeps the previous value instead of
        guessing."""
        try:
            v = float(text)
        except (ValueError, TypeError):
            return fallback_ms
        return v * 1000.0 if self.time_unit.get() == "s" else v

    def get_flash_intensity_int(self):
        try:
            return max(1, min(255, int(float(self.flash_intensity.get()))))
        except ValueError:
            return 255

    def get_pin_int(self):
        try:
            return int(self.pin.get())
        except ValueError:
            return DEFAULT_PINS[self.index]

    def to_dict(self):
        on_ms, dark_ms, _continuous = self.get_pulse_timing()
        return {
            "enabled":  self.enabled.get(),
            "pulsed_intensity_percent": self.get_duty_pct(),
            "on_time_ms":  on_ms,
            "off_time_ms": dark_ms,
            "flash_intensity": self.get_flash_intensity_int(),
            "duration": self.duration.get(),
            "pin":      self.pin.get(),
            "time_unit": self.time_unit.get(),
            "force_continuous": self.force_continuous.get(),
        }

    def from_dict(self, d):
        self.enabled.set(d.get("enabled", False))
        self.duration.set(d.get("duration", 0.0))
        self.pin.set(str(d.get("pin", DEFAULT_PINS[self.index])))
        self.time_unit.set(d.get("time_unit", "ms"))
        self.force_continuous.set(d.get("force_continuous", False))
        if "on_time_ms" in d and "off_time_ms" in d:
            self.on_time_ms.set(d.get("on_time_ms", DEFAULT_ON_MS))
            self.off_time_ms.set(d.get("off_time_ms", DEFAULT_ON_MS))
            self.pulsed_intensity.set(round(self.get_duty_pct(), 1))
        else:
            # Old-format protocol (saved before On/Off ms existed, just a
            # duty %) -- reconstruct timing at the old fixed 250ms on-pulse
            # so it plays back the same as when it was saved.
            self.on_time_ms.set(DEFAULT_ON_MS)
            self.apply_duty_pct(d.get("pulsed_intensity_percent", 50.0))
        # Old protocols saved before this field existed get full brightness
        # (255), matching the always-full-PWM behavior they were saved with.
        self.flash_intensity.set(str(d.get("flash_intensity", 255)))
        self.refresh_time_display()


class BoltGUI:
    def __init__(self, root):
        self.root = root
        self.theme = "light"
        self.C = dict(PALETTES[self.theme])

        self.root.title("B.O.L.T. — Budget Optogenetic Light Tool")
        self.root.geometry("1034x700")   # fits all 4 channel panels at full size on open
        # Window can't shrink smaller than this, or the panels/buttons get clipped.
        self.root.minsize(1012, 656)
        self.root.configure(bg=self.C["BG_DARK"])
        self.ser = None

        self.channels = [Channel(i) for i in range(NUM_CHANNELS)]

        self.protocol_name  = tk.StringVar(value="")
        self.protocol_notes = tk.StringVar(value="")

        self.use_global_duration = tk.BooleanVar(value=True)
        self.global_dur_h = tk.IntVar(value=0)
        self.global_dur_m = tk.IntVar(value=0)
        self.global_dur_s = tk.IntVar(value=0)

        self.delay_enabled = tk.BooleanVar(value=False)
        self.delay_h = tk.IntVar(value=0)
        self.delay_m = tk.IntVar(value=0)
        self.delay_s = tk.IntVar(value=0)

        self.port_var = tk.StringVar()
        self.port_map = {}   # display label -> actual device path (e.g. "COM6")

        self.recent_paths = self._load_recent()
        self.recent_var = tk.StringVar(value="Select…")

        # Simulation state per channel
        self.sim_running   = False
        self.sim_is_on     = [False] * NUM_CHANNELS
        self.sim_phase_start = [0.0] * NUM_CHANNELS
        self.sim_speed        = tk.StringVar(value="1")   # simulation speed multiplier
        self.sim_virtual_now  = 0.0
        self.sim_last_real    = 0.0
        self.duty_time_var    = tk.StringVar(value="t = 0:00")

        self._build_style()
        self._build_ui()
        self._refresh_ports()
        self._refresh_recent_combo()
        self._update_schematic()   # draw initial state
        self._update_duty_graph()

    # ================================================================ STYLE

    def _build_style(self):
        C = self.C
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=C["BG_DARK"], foreground=C["FG_TEXT"],
                         font=FONT_BASE)
        style.configure("TFrame", background=C["BG_DARK"])
        style.configure("Header.TFrame", background=C["BG_PANEL"])
        style.configure("TLabelframe", background=C["BG_PANEL"], foreground=C["FG_TEXT"],
                         borderwidth=1, relief="solid", bordercolor=C["BORDER"])
        style.configure("TLabelframe.Label", background=C["BG_PANEL"],
                         foreground=C["FG_TEXT"], font=(FONT_FAMILY, 10, "bold"))
        style.configure("Separator.TSeparator", background=C["BORDER_SOFT"])
        style.configure("TLabel", background=C["BG_PANEL"], foreground=C["FG_TEXT"])
        style.configure("Muted.TLabel", background=C["BG_PANEL"], foreground=C["FG_MUTED"],
                         font=(FONT_FAMILY, 9))
        style.configure("Header.TLabel", background=C["BG_PANEL"], foreground=C["FG_TEXT"],
                         font=FONT_LOGO)
        style.configure("Sub.TLabel", background=C["BG_PANEL"], foreground=C["FG_MUTED"],
                         font=(FONT_FAMILY, 9))
        style.configure("TCheckbutton", background=C["BG_PANEL"], foreground=C["FG_TEXT"])
        style.map("TCheckbutton", background=[("active", C["BG_PANEL"])])

        # lightcolor/darkcolor draw this theme's 3D border -- left unset,
        # they default to a light shade that looks wrong in dark mode.
        style.configure("TEntry", fieldbackground=C["ENTRY_BG"], foreground=C["FG_TEXT"],
                         insertcolor=C["FG_TEXT"], borderwidth=1, bordercolor=C["BORDER"],
                         lightcolor=C["BORDER"], darkcolor=C["BORDER"])
        style.map("TEntry",
                   bordercolor=[("focus", C["ACCENT"])],
                   lightcolor=[("focus", C["ACCENT"])],
                   darkcolor=[("focus", C["ACCENT"])],
                   # Default disabled text is a near-invisible near-white on dark mode.
                   foreground=[("disabled", C["FG_MUTED"])],
                   fieldbackground=[("disabled", C["BG_PANEL"])])
        style.configure("TSpinbox", fieldbackground=C["ENTRY_BG"], foreground=C["FG_TEXT"],
                         insertcolor=C["FG_TEXT"], borderwidth=1, bordercolor=C["BORDER"],
                         lightcolor=C["BORDER"], darkcolor=C["BORDER"], arrowcolor=C["FG_TEXT"],
                         background=C["BTN_BG"])
        style.map("TSpinbox",
                   bordercolor=[("focus", C["ACCENT"])],
                   lightcolor=[("focus", C["ACCENT"])],
                   darkcolor=[("focus", C["ACCENT"])],
                   foreground=[("disabled", C["FG_MUTED"])],
                   fieldbackground=[("disabled", C["BG_PANEL"])],
                   arrowcolor=[("disabled", C["BORDER"])])
        style.configure("TCombobox", fieldbackground=C["ENTRY_BG"], foreground=C["FG_TEXT"],
                         background=C["ENTRY_BG"], arrowcolor=C["FG_TEXT"],
                         bordercolor=C["BORDER"], lightcolor=C["BORDER"], darkcolor=C["BORDER"])
        style.map("TCombobox",
                   fieldbackground=[("readonly", C["ENTRY_BG"])],
                   foreground=[("readonly", C["FG_TEXT"])],
                   bordercolor=[("focus", C["ACCENT"])],
                   lightcolor=[("focus", C["ACCENT"])],
                   darkcolor=[("focus", C["ACCENT"])],
                   background=[("readonly", C["ENTRY_BG"]), ("active", C["BTN_BG"])])
        is_light = self.theme == "light"

        style.configure("TButton", background=C["BTN_BG"], foreground=C["FG_TEXT"],
                         font=FONT_BASE_B, padding=6, borderwidth=0)
        style.map("TButton", background=[("active", C["ACCENT_DIM"])])

        accent_active = "#4a75e8" if is_light else "#6ea8ff"
        style.configure("Accent.TButton", background=C["ACCENT"], foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", accent_active)])

        stop_active = "#e0576a" if is_light else "#ff7b72"
        style.configure("Stop.TButton", background=C["ERR_RED"], foreground="#ffffff")
        style.map("Stop.TButton", background=[("active", stop_active)])

        send_fg = "#ffffff" if is_light else "#04160c"
        send_active = "#27b164" if is_light else "#56d364"
        style.configure("Send.TButton", background=C["OK_GREEN"], foreground=send_fg)
        style.map("Send.TButton", background=[("active", send_active)])

        self.root.option_add("*Font", FONT_BASE)
        self.root.option_add("*Menu.Font", FONT_BASE)

        # The dropdown list itself isn't a themeable ttk widget, so it needs
        # colors set this separate way or it stays plain white in dark mode.
        self.root.option_add("*TCombobox*Listbox.background", C["ENTRY_BG"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["FG_TEXT"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["ACCENT"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", FONT_BASE)

    # ================================================================ UI BUILD

    def _build_ui(self):
        pad = {"padx": 6, "pady": 2}
        C = self.C

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # ---- Header: logo + title + theme toggle ----------------------
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        self.logo_canvas = tk.Canvas(header, width=30, height=30, bg=C["BG_PANEL"],
                                      highlightthickness=0)
        self.logo_canvas.grid(row=0, column=0, padx=(14, 8), pady=5)
        self._draw_logo(self.logo_canvas)

        title_box = ttk.Frame(header, style="Header.TFrame")
        title_box.grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(title_box, text="B.O.L.T.", style="Header.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="BUDGET OPTOGENETIC LIGHT TOOL",
                  style="Sub.TLabel").pack(anchor="w")

        self.theme_btn = ttk.Button(header, text="🌙  Dark", command=self._toggle_theme)
        self.theme_btn.grid(row=0, column=2, padx=14, pady=5, sticky="e")

        self.header_rule = tk.Frame(self.root, bg=C["BORDER"], height=1)
        self.header_rule.grid(row=1, column=0, sticky="ew")

        # ---- Body container --------------------------------------------------
        body = ttk.Frame(self.root)
        body.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 3))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        # ---- Connection + Protocol (combined into one box to save a row) -----
        conn = ttk.LabelFrame(body, text="Connection", padding=(4, 2))
        conn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        conn.columnconfigure(0, weight=1)

        conn_row = ttk.Frame(conn, style="Header.TFrame")
        conn_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(conn_row, text="Port:").grid(row=0, column=0, **pad)
        self.port_combo = ttk.Combobox(conn_row, textvariable=self.port_var,
                                        width=24, state="readonly")
        self.port_combo.grid(row=0, column=1, **pad)
        ttk.Button(conn_row, text="Refresh",
                   command=self._refresh_ports).grid(row=0, column=2, **pad)
        self.connect_btn = ttk.Button(conn_row, text="Connect", style="Accent.TButton",
                                       command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=3, **pad)
        ttk.Separator(conn_row, orient="vertical").grid(row=0, column=4, sticky="ns", pady=3)
        self.status_label = ttk.Label(conn_row, text="●  Disconnected", foreground=C["ERR_RED"],
                                       font=FONT_BASE_B)
        self.status_label.grid(row=0, column=5, padx=(10, 6), pady=2)

        proto_row = ttk.Frame(conn, style="Header.TFrame")
        proto_row.grid(row=1, column=0, sticky="ew")
        ttk.Label(proto_row, text="Name:").grid(row=0, column=0, **pad)
        ttk.Entry(proto_row, textvariable=self.protocol_name, width=16
                  ).grid(row=0, column=1, **pad)
        ttk.Label(proto_row, text="Notes:").grid(row=0, column=2, **pad)
        ttk.Entry(proto_row, textvariable=self.protocol_notes
                  ).grid(row=0, column=3, sticky="ew", **pad)
        proto_row.columnconfigure(3, weight=1)
        self.log_indicator = ttk.Label(conn_row, text="No messages yet", style="Muted.TLabel")
        self.log_indicator.grid(row=0, column=6, padx=(6, 4), pady=2, sticky="w")
        conn_row.columnconfigure(6, weight=1)

        # ---- Right column: LED schematic + simulate -----------------------
        # Must be built first -- the channel panels below trigger a redraw
        # of this canvas as soon as they exist.
        right = ttk.LabelFrame(body, text="LED Strip Preview", padding=(4, 3))
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(right, bg=C["BG_CANVAS"], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=(4, 4))
        self.canvas.bind("<Configure>", self._update_schematic)

        sim_row = ttk.Frame(right, style="Header.TFrame")
        sim_row.grid(row=1, column=0, pady=(0, 4))
        self.sim_btn = ttk.Button(sim_row, text="▶  Simulate",
                                   command=self._toggle_simulation)
        self.sim_btn.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(sim_row, text="Speed:", style="Sub.TLabel"
                  ).grid(row=0, column=1, padx=(0, 3))
        speed_entry = ttk.Entry(sim_row, textvariable=self.sim_speed, width=4,
                                 justify="center")
        speed_entry.grid(row=0, column=2)
        speed_entry.bind("<FocusOut>", lambda e: self._validate_sim_speed())
        speed_entry.bind("<Return>",   lambda e: self._validate_sim_speed())
        ttk.Label(sim_row, text="×", style="Sub.TLabel"
                  ).grid(row=0, column=3, padx=(2, 10))
        ttk.Label(sim_row, textvariable=self.duty_time_var, style="Sub.TLabel"
                  ).grid(row=0, column=4)

        ttk.Separator(right, orient="horizontal").grid(
            row=2, column=0, sticky="ew", padx=8, pady=(0, 2))

        self.duty_canvas = tk.Canvas(right, bg=C["BG_PANEL"], height=64,
                                      highlightthickness=0)
        self.duty_canvas.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
        self.duty_canvas.bind("<Configure>", self._update_duty_graph)
        right.rowconfigure(3, weight=0)

        # ---- Left column: channel panels, static 2x2 grid ------------------
        left = ttk.Frame(body)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=1)
        left.rowconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        self.channel_widgets = []
        for i, ch in enumerate(self.channels):
            r, c = divmod(i, 2)
            self._build_channel_panel(left, ch, i, r, c)

        # ---- Global settings (single row: duration + delay side by side) -------
        glob = ttk.LabelFrame(body, text="Experiment Settings", padding=(4, 2))
        glob.grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)

        ttk.Checkbutton(glob, text="Same duration for all channels",
                         variable=self.use_global_duration,
                         command=self._on_global_duration_toggle
                         ).grid(row=0, column=0, sticky="w", **pad)
        self.global_dur_entries = self._build_hms_entries(
            glob, 0, 1, self.global_dur_h, self.global_dur_m, self.global_dur_s)
        ttk.Label(glob, text="(h:m:s, 0=∞)", style="Muted.TLabel"
                  ).grid(row=0, column=2, sticky="w", padx=(2, 10), pady=2)

        ttk.Separator(glob, orient="vertical").grid(row=0, column=3, sticky="ns", pady=2)

        delay_check = ttk.Checkbutton(glob, text="Delayed start",
                                       variable=self.delay_enabled)
        delay_check.grid(row=0, column=4, sticky="w", padx=(10, 5), pady=2)
        self._build_hms_entries(
            glob, 0, 5, self.delay_h, self.delay_m, self.delay_s)
        ttk.Label(glob, text="(h:m:s) after Start", style="Muted.TLabel"
                  ).grid(row=0, column=6, sticky="w", padx=(2, 5), pady=2)

        # ---- Action buttons ------------------------------------------------------
        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 2))

        ttk.Button(actions, text="Send Protocol to Arduino", style="Send.TButton",
                   command=self._send_protocol).grid(row=0, column=0, padx=(0, 6), ipadx=2)
        ttk.Button(actions, text="▶  Start", style="Accent.TButton",
                   command=self._start_experiment).grid(row=0, column=1, padx=6, ipadx=2)
        ttk.Button(actions, text="■  STOP", style="Stop.TButton",
                   command=self._stop_experiment).grid(row=0, column=2, padx=6, ipadx=2)
        ttk.Separator(actions, orient="vertical").grid(row=0, column=3, sticky="ns", padx=6)
        ttk.Button(actions, text="Save…",
                   command=self._save_protocol).grid(row=0, column=4, padx=6)
        ttk.Button(actions, text="Load…",
                   command=self._load_protocol).grid(row=0, column=5, padx=(6, 6))
        ttk.Label(actions, text="Recent:").grid(row=0, column=6, padx=(0, 3))
        self.recent_combo = ttk.Combobox(actions, textvariable=self.recent_var,
                                          width=16, state="readonly")
        self.recent_combo.grid(row=0, column=7)
        self.recent_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)

        self._on_global_duration_toggle()
        self._check_pin_conflicts()

    def _draw_logo(self, canvas):
        """Simple mark: a lightning bolt through a strip outline."""
        canvas.create_oval(2, 2, 28, 28, outline=self.C["ACCENT"], width=2)
        pts = [15, 6, 9, 16, 14, 16, 11, 24, 20, 12, 15, 12]
        canvas.create_polygon(pts, fill=self.C["ACCENT"], outline="")

    def _build_hms_entries(self, parent, row, col, h_var, m_var, s_var):
        """A compact H : M : S entry trio sharing one grid cell."""
        box = ttk.Frame(parent, style="Header.TFrame")
        box.grid(row=row, column=col, sticky="w")
        entries = []
        for j, var in enumerate((h_var, m_var, s_var)):
            if j > 0:
                ttk.Label(box, text=":", style="TLabel").grid(row=0, column=j * 2 - 1)
            is_hour = (j == 0)
            entry = ttk.Entry(box, textvariable=var, width=3, justify="center")
            entry.grid(row=0, column=j * 2)
            entry.bind("<FocusOut>", lambda e, v=var, h=is_hour: self._clamp_hms_field(v, h))
            entry.bind("<Return>",   lambda e, v=var, h=is_hour: self._clamp_hms_field(v, h))
            entries.append(entry)
        return entries

    def _safe_int(self, var, default=0):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    def _clamp_hms_field(self, var, is_hour):
        v = self._safe_int(var, 0)
        v = max(0, min(99 if is_hour else 59, v))
        var.set(v)

    def _get_global_duration_secs(self):
        return (self._safe_int(self.global_dur_h) * 3600
                + self._safe_int(self.global_dur_m) * 60
                + self._safe_int(self.global_dur_s))

    def _get_delay_secs(self):
        return (self._safe_int(self.delay_h) * 3600
                + self._safe_int(self.delay_m) * 60
                + self._safe_int(self.delay_s))

    def _build_channel_panel(self, parent, ch, i, row, col):
        frame = ttk.LabelFrame(parent, text=f"LED {i + 1}", padding=(8, 6))
        frame.grid(row=row, column=col, sticky="nsew",
                    padx=(0, 6) if col == 0 else (6, 0),
                    pady=(0, 6) if row == 0 else (6, 0))
        for c in range(5):
            frame.columnconfigure(c, weight=1)

        pad = {"padx": 4, "pady": 4}

        # Enable toggle + per-flash PWM level + pin selector
        ttk.Checkbutton(frame, text="Enabled", variable=ch.enabled,
                         command=self._on_channel_visual_change
                         ).grid(row=0, column=0, sticky="w", **pad)
        flash_label = ttk.Label(frame, text="Intensity:")
        flash_label.grid(row=0, column=1, sticky="e", padx=(8, 2), pady=4)
        flash_entry = ttk.Entry(frame, textvariable=ch.flash_intensity, width=4)
        flash_entry.grid(row=0, column=2, sticky="w", padx=(0, 2), pady=4)
        flash_entry.bind("<FocusOut>", lambda e, idx=i: self._validate_flash_entry(idx))
        flash_entry.bind("<Return>",   lambda e, idx=i: self._validate_flash_entry(idx))
        pin_label = ttk.Label(frame, text="Pin:")
        pin_label.grid(row=0, column=3, sticky="e", padx=(8, 2), pady=4)
        pin_combo = ttk.Combobox(frame, textvariable=ch.pin, width=3,
                                  state="readonly",
                                  values=[str(p) for p in PWM_PINS])
        pin_combo.grid(row=0, column=4, sticky="w", padx=(0, 2), pady=4)
        pin_combo.bind("<<ComboboxSelected>>", lambda e: (self._check_pin_conflicts(),
                                                            self._update_schematic()))

        ttk.Separator(frame, orient="horizontal").grid(
            row=1, column=0, columnspan=5, sticky="ew", pady=(4, 4))

        # Duty cycle: what % of the time this channel is lit. Typing a
        # new value here recomputes the Off time below to match.
        # Continuous overrides all of this and keeps the light on full-time.
        duty_row = ttk.Frame(frame)
        duty_row.grid(row=2, column=0, columnspan=5, sticky="w", padx=4, pady=(2, 2))
        ttk.Label(duty_row, text="Duty cycle:").pack(side="left")
        duty_spin = ttk.Spinbox(duty_row, from_=0.5, to=100.0, increment=1.0,
                                 textvariable=ch.pulsed_intensity, width=6,
                                 command=lambda idx=i: self._on_duty_committed(idx))
        duty_spin.pack(side="left", padx=(4, 2))
        duty_spin.bind("<FocusOut>", lambda e, idx=i: self._on_duty_committed(idx))
        duty_spin.bind("<Return>",   lambda e, idx=i: self._on_duty_committed(idx))
        ttk.Label(duty_row, text="%").pack(side="left")

        cont_check = ttk.Checkbutton(duty_row, text="Continuous",
                                      variable=ch.force_continuous,
                                      command=lambda idx=i: self._on_continuous_toggled(idx))
        cont_check.pack(side="left", padx=(14, 0))

        # On/Off: how long each flash lasts and how long the dark gap
        # between flashes is, in whichever unit (ms or s) is picked.
        # Typing a new value here updates the duty cycle (%) above.
        onoff_row = ttk.Frame(frame)
        onoff_row.grid(row=3, column=0, columnspan=5, sticky="w", padx=4, pady=(2, 2))
        ttk.Label(onoff_row, text="On:").pack(side="left")
        on_entry = ttk.Entry(onoff_row, textvariable=ch.on_time_display, width=6)
        on_entry.pack(side="left", padx=(4, 8))
        on_entry.bind("<FocusOut>", lambda e, idx=i: self._on_time_committed(idx))
        on_entry.bind("<Return>",   lambda e, idx=i: self._on_time_committed(idx))

        ttk.Label(onoff_row, text="Off:").pack(side="left")
        off_entry = ttk.Entry(onoff_row, textvariable=ch.off_time_display, width=6)
        off_entry.pack(side="left", padx=(4, 8))
        off_entry.bind("<FocusOut>", lambda e, idx=i: self._off_time_committed(idx))
        off_entry.bind("<Return>",   lambda e, idx=i: self._off_time_committed(idx))

        unit_combo = ttk.Combobox(onoff_row, textvariable=ch.time_unit, width=3,
                                   state="readonly", values=["ms", "s"])
        unit_combo.pack(side="left")
        unit_combo.bind("<<ComboboxSelected>>", lambda e, idx=i: self._on_unit_changed(idx))

        # Fixed width so the status text's length changing (as values
        # change) never nudges the panel's/column's layout.
        pulsed_info_label = ttk.Label(frame, text="", style="Muted.TLabel", width=22)
        pulsed_info_label.grid(row=4, column=0, columnspan=5, sticky="w", padx=4, pady=(0, 4))

        dur_sep = ttk.Separator(frame, orient="horizontal")
        dur_sep.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(1, 4))

        # This channel's own duration -- only used if "Same duration for
        # all channels" is unticked. Stays visible but greyed out
        # otherwise, so the layout doesn't jump around when toggling it.
        dur_label = ttk.Label(frame, text="Duration (min, 0=∞):")
        dur_label.grid(row=6, column=0, columnspan=2, sticky="e", **pad)
        dur_entry = ttk.Entry(frame, textvariable=ch.duration, width=6)
        dur_entry.grid(row=6, column=2, sticky="w")

        self.channel_widgets.append({
            "dur_label": dur_label, "dur_entry": dur_entry,
            "pin_label": pin_label, "flash_label": flash_label, "flash_entry": flash_entry,
            "pulsed_info_label": pulsed_info_label,
            "duty_spin": duty_spin, "on_entry": on_entry, "off_entry": off_entry,
            "unit_combo": unit_combo, "cont_check": cont_check,
        })
        self._refresh_pulse_widgets(i)

    # ============================================================ VALIDATION

    def _validate_flash_entry(self, i):
        ch = self.channels[i]
        try:
            v = int(float(ch.flash_intensity.get()))
        except (tk.TclError, ValueError):
            v = 255
        ch.flash_intensity.set(str(max(1, min(255, v))))
        self._on_channel_visual_change()

    def _on_duty_committed(self, i):
        ch = self.channels[i]
        try:
            v = float(ch.pulsed_intensity.get())
        except (tk.TclError, ValueError):
            v = 50.0
        ch.apply_duty_pct(round(max(0.5, min(100.0, v)) * 2) / 2)
        self._refresh_pulse_widgets(i)
        self._on_channel_visual_change()

    def _on_time_committed(self, i):
        ch = self.channels[i]
        ms = ch.parse_time_input(ch.on_time_display.get(), ch.on_time_ms.get())
        ch.on_time_ms.set(max(1.0, round(ms, 1)))
        ch.sync_duty_from_on_off()
        self._refresh_pulse_widgets(i)
        self._on_channel_visual_change()

    def _off_time_committed(self, i):
        ch = self.channels[i]
        ms = ch.parse_time_input(ch.off_time_display.get(), ch.off_time_ms.get())
        ch.off_time_ms.set(max(0.0, round(ms, 1)))
        ch.sync_duty_from_on_off()
        self._refresh_pulse_widgets(i)
        self._on_channel_visual_change()

    def _on_unit_changed(self, i):
        self._refresh_pulse_widgets(i)

    def _on_continuous_toggled(self, i):
        ch = self.channels[i]
        if not ch.force_continuous.get():
            # Unticked -- restore the duty% display to match whatever
            # pulsed on/off timing was kept underneath, rather than
            # leaving it at the leftover forced-100 value.
            ch.sync_duty_from_on_off()
        self._refresh_pulse_widgets(i)
        self._on_channel_visual_change()

    def _refresh_pulse_widgets(self, i):
        ch = self.channels[i]
        w  = self.channel_widgets[i]
        on_ms, dark_ms, continuous = ch.get_pulse_timing()
        if continuous:
            ch.pulsed_intensity.set(100.0)
        ch.refresh_time_display()

        w["on_entry"].config(state="disabled" if continuous else "normal")
        forced_state = "disabled" if ch.force_continuous.get() else "normal"
        w["off_entry"].config(state=forced_state)
        w["duty_spin"].config(state=forced_state)

        status = "continuous" if continuous else f"≈{ch.get_duty_pct():.1f}% duty"
        w["pulsed_info_label"].config(text=status)

    def _on_channel_visual_change(self):
        self._update_schematic()
        self._update_duty_graph()

    def _on_global_duration_toggle(self):
        self._refresh_duration_fields()
        state = "normal" if self.use_global_duration.get() else "disabled"
        for entry in self.global_dur_entries:
            entry.config(state=state)

    def _refresh_duration_fields(self):
        # Per-channel duration is only meaningful when NOT using one shared
        # duration — greyed out and disabled the rest of the time (the
        # common case), rather than hidden, so the panel layout never
        # shifts when toggling "Same duration for all channels".
        active = not self.use_global_duration.get()
        state = "normal" if active else "disabled"
        label_fg = self.C["FG_TEXT"] if active else self.C["FG_MUTED"]
        for w in self.channel_widgets:
            w["dur_entry"].config(state=state)
            w["dur_label"].config(foreground=label_fg)

    def _check_pin_conflicts(self):
        """Flag any channel whose pin is reused by another channel."""
        pins = [ch.get_pin_int() for ch in self.channels]
        for i, p in enumerate(pins):
            conflict = pins.count(p) > 1
            self.channel_widgets[i]["pin_label"].config(
                text="Pin ⚠" if conflict else "Pin:",
                foreground=self.C["ERR_RED"] if conflict else self.C["FG_TEXT"])

    # ============================================================ THEME

    def _toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self.C = dict(PALETTES[self.theme])
        self.theme_btn.config(text="☀  Light" if self.theme == "dark" else "🌙  Dark")
        self._apply_theme()

    def _apply_theme(self):
        C = self.C
        self._build_style()
        self.root.configure(bg=C["BG_DARK"])
        self.logo_canvas.configure(bg=C["BG_PANEL"])
        self.logo_canvas.delete("all")
        self._draw_logo(self.logo_canvas)
        self.header_rule.configure(bg=C["BORDER"])
        self.canvas.configure(bg=C["BG_CANVAS"])
        self.duty_canvas.configure(bg=C["BG_PANEL"])

        self._refresh_status_label()

        self._check_pin_conflicts()
        self._refresh_duration_fields()
        self._update_schematic()
        self._update_duty_graph()

    # ============================================================ SCHEMATIC

    def _intensity_color(self, pct):
        """Picks a strip color for a given brightness % -- brighter
        settings look more strongly colored, dimmer ones paler."""
        t = max(0.0, min(100.0, pct)) / 100.0
        t = 0.35 + 0.65 * t
        base = tuple(int(self.C["BG_CANVAS"][j:j + 2], 16) for j in (1, 3, 5))
        accent = tuple(int(self.C["ACCENT"][j:j + 2], 16) for j in (1, 3, 5))
        r = int(base[0] + (accent[0] - base[0]) * t)
        g = int(base[1] + (accent[1] - base[1]) * t)
        b = int(base[2] + (accent[2] - base[2]) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _update_schematic(self, _event=None):
        """Redraw the LED strip schematic: 4 simple strip outlines."""
        c = self.canvas
        c.delete("all")
        C = self.C

        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2 or h < 2:
            self.root.after(30, self._update_schematic)
            return

        margin_x = min(24, w * 0.06)
        margin_y = min(14, h * 0.05)
        gap      = min(10, h * 0.04)
        strip_w  = w - 2 * margin_x
        strip_h  = (h - 2 * margin_y - gap * (NUM_CHANNELS - 1)) / NUM_CHANNELS
        strip_h  = max(strip_h, 4)

        for i, ch in enumerate(self.channels):
            y_top = margin_y + i * (strip_h + gap)
            y_bot = y_top + strip_h
            x_l   = margin_x
            x_r   = margin_x + strip_w

            pwm_pct = (ch.get_flash_intensity_int() / 255.0 * 100.0) if ch.enabled.get() else 0
            lit = ch.enabled.get() and (self.sim_is_on[i] or not self.sim_running)
            # Shaded by brightness only (not how often it flashes) -- so
            # the color always shows how strong the light is when lit.
            fill_color = self._intensity_color(pwm_pct) if lit else C["STRIP_OFF"]
            outline_color = C["ACCENT"] if lit else C["STRIP_OFF"]

            c.create_rectangle(x_l, y_top, x_r, y_bot,
                                fill=fill_color, outline=outline_color, width=2)

            label_col = "#ffffff" if lit else (C["FG_TEXT"] if ch.enabled.get() else C["FG_MUTED"])
            c.create_text(x_l + 10, y_top + strip_h / 2,
                           text=f"LED {i+1}  ·  pin {ch.get_pin_int()}",
                           fill=label_col, anchor="w",
                           font=("Consolas", 9, "bold"))

            if ch.enabled.get():
                pwm_val = ch.get_flash_intensity_int()
                duty_disp = max(round(ch.get_duty_pct()), 1)
                c.create_text(x_r - 10, y_top + strip_h / 2,
                               text=f"PWM {pwm_val} · {duty_disp}% duty cycle",
                               fill=label_col, anchor="e",
                               font=("Consolas", 9))

    def _update_duty_graph(self, _event=None):
        """Draws one row per channel showing its flash pattern to scale:
        a solid bar for continuous channels, or the real on/off widths
        for pulsed ones."""
        c = self.duty_canvas
        c.delete("all")
        C = self.C

        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2 or h < 2:
            self.root.after(30, self._update_duty_graph)
            return

        margin_l, margin_r = 42, 8
        margin_t, margin_b = 14, 4
        row_h = (h - margin_t - margin_b) / NUM_CHANNELS

        periods = []
        for ch in self.channels:
            if ch.enabled.get():
                on_ms, dark_ms, continuous = ch.get_pulse_timing()
                if not continuous:
                    periods.append((on_ms + dark_ms) / 1000.0)
        # Show 4 repeats of the slowest channel's cycle, so the pattern
        # actually looks repeating rather than one pulse in empty space.
        CYCLES_SHOWN = 4
        window = max(periods) * CYCLES_SHOWN if periods else 1.0
        window = max(0.3, window)
        # Cap on segments drawn per channel, in case a very fast channel
        # would otherwise need thousands of tiny rectangles to fill the window.
        max_segments = 2000

        if periods:
            if window >= 1.0:
                mins, secs = divmod(int(window), 60)
                span_txt = f"{mins}:{secs:02d}" if mins else f"{secs}s"
            else:
                span_txt = f"{window * 1000:.0f}ms"
            c.create_text(w - margin_r, 2, text=f"window: {span_txt}",
                          anchor="ne", fill=C["FG_MUTED"], font=("Consolas", 8))

        for i, ch in enumerate(self.channels):
            y0 = margin_t + i * row_h
            y1 = y0 + row_h - 3
            x0 = margin_l
            x1 = w - margin_r

            c.create_rectangle(x0, y0, x1, y1, fill=C["BG_CANVAS"],
                                outline=C["BORDER_SOFT"])
            c.create_text(2, (y0 + y1) / 2, text=f"LED{i + 1}", anchor="w",
                          fill=C["FG_MUTED"], font=("Consolas", 8, "bold"))

            if ch.enabled.get():
                # Colored by peak brightness alone -- duty cycle is already
                # shown by the width/spacing of the segments themselves.
                flash_pct = ch.get_flash_intensity_int() / 255.0 * 100.0
                color = self._intensity_color(flash_pct)
                on_ms, dark_ms, continuous = ch.get_pulse_timing()
                if continuous:
                    c.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
                else:
                    on_t = on_ms / 1000.0
                    period = (on_ms + dark_ms) / 1000.0
                    t = 0.0
                    segments = 0
                    while t < window and segments < max_segments:
                        seg_end = min(t + on_t, window)
                        sx0 = x0 + (t / window) * (x1 - x0)
                        sx1 = x0 + (seg_end / window) * (x1 - x0)
                        # Guarantee a sliver is visible even when the
                        # on-pulse is a tiny fraction of a long window.
                        sx1 = max(sx1, sx0 + 1)
                        c.create_rectangle(sx0, y0, sx1, y1, fill=color, outline="")
                        t += period
                        segments += 1

        if self.sim_running and periods:
            pos = (self.sim_virtual_now % window) / window
            cx = margin_l + pos * (w - margin_l - margin_r)
            c.create_line(cx, 0, cx, h, fill=C["ACCENT"], width=2)

    # ============================================================ SIMULATION

    def _toggle_simulation(self):
        if self.sim_running:
            self._stop_simulation()
        else:
            self._start_simulation()

    def _get_sim_speed(self):
        try:
            v = float(self.sim_speed.get())
            return max(0.1, min(100.0, v))
        except ValueError:
            return 1.0

    def _validate_sim_speed(self):
        v = self._get_sim_speed()
        formatted = f"{v:g}"
        if self.sim_speed.get() != formatted:
            self.sim_speed.set(formatted)

    def _start_simulation(self):
        self._validate_sim_speed()
        self.sim_running = True
        self.sim_btn.config(text="■  Stop preview")
        self.sim_virtual_now = 0.0
        self.sim_last_real   = time.time()
        for i in range(NUM_CHANNELS):
            self.sim_is_on[i]      = True
            self.sim_phase_start[i] = 0.0
        self._sim_tick()

    def _stop_simulation(self):
        self.sim_running = False
        self.sim_btn.config(text="▶  Simulate")
        self.sim_is_on = [False] * NUM_CHANNELS
        self.duty_time_var.set("t = 0:00")
        self._update_schematic()
        self._update_duty_graph()

    def _sim_tick(self):
        if not self.sim_running:
            return
        real_now = time.time()
        dt = real_now - self.sim_last_real
        self.sim_last_real = real_now
        self.sim_virtual_now += dt * self._get_sim_speed()
        now = self.sim_virtual_now

        for i, ch in enumerate(self.channels):
            if not ch.enabled.get():
                self.sim_is_on[i] = False
                continue
            on_ms, dark_ms, continuous = ch.get_pulse_timing()
            if continuous:
                self.sim_is_on[i] = True
                continue
            elapsed = now - self.sim_phase_start[i]
            on_t  = on_ms / 1000.0
            off_t = max(0.001, dark_ms / 1000.0)
            if self.sim_is_on[i] and elapsed >= on_t:
                self.sim_is_on[i]       = False
                self.sim_phase_start[i] = now
            elif not self.sim_is_on[i] and elapsed >= off_t:
                self.sim_is_on[i]       = True
                self.sim_phase_start[i] = now

        mins, secs = divmod(int(now), 60)
        self.duty_time_var.set(f"t = {mins}:{secs:02d}  (×{self._get_sim_speed():g})")
        self._update_schematic()
        self._update_duty_graph()
        self.root.after(80, self._sim_tick)   # ~12 fps refresh

    # ============================================================== SERIAL

    @staticmethod
    def _find_arduino_port(infos):
        """Prefer a port whose USB descriptor looks like an Arduino Uno or a
        common Uno-compatible board (e.g. Elegoo), over an arbitrary port
        (Bluetooth serial, etc.)."""
        for p in infos:
            if p.vid in ARDUINO_VIDS:
                return p
        for p in infos:
            if p.description and "arduino" in p.description.lower():
                return p
        return None

    @staticmethod
    def _port_label(p):
        """Human-readable label like 'Arduino Uno (COM6)' instead of a bare
        port name — falls back to the raw device if no useful description
        is available (some platforms/ports report none)."""
        desc = (p.description or "").strip()
        if not desc or desc.upper() == "N/A" or desc == p.device:
            return p.device
        suffix = f"({p.device})"
        if desc.endswith(suffix):
            return desc
        return f"{desc} {suffix}"

    @staticmethod
    def _is_arduino_like(p):
        return bool(p.vid in ARDUINO_VIDS
                     or (p.description and "arduino" in p.description.lower()))

    def _refresh_ports(self):
        # Only list ports that look like an Arduino/Uno-compatible board —
        # unrelated serial devices (Bluetooth COM ports, etc.) never show up
        # as connectable options.
        infos = [p for p in serial.tools.list_ports.comports()
                  if self._is_arduino_like(p)]
        self.port_map = {self._port_label(p): p.device for p in infos}
        labels = list(self.port_map.keys())
        self.port_combo["values"] = labels

        # If the port we're connected to has physically disappeared (device
        # unplugged), the socket doesn't fail on its own until the next
        # read/write — detect that here rather than showing a stale "Connected".
        if self.ser and self.ser.is_open and self.ser.port not in [p.device for p in infos]:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.connect_btn.config(text="Connect")

        current = self.port_var.get()
        if current and current not in labels:
            current = ""   # previously selected port is no longer plugged in / valid

        if not current:
            auto = self._find_arduino_port(infos)
            if auto:
                self.port_var.set(self._port_label(auto))
            elif labels:
                self.port_var.set(labels[0])
            else:
                self.port_var.set(NO_PORT_LABEL)

    def _refresh_status_label(self, infos=None):
        """Updates the one connection status label: shows Connected, or a
        detected-but-not-connected port, or Disconnected."""
        C = self.C
        if infos is None:
            infos = [p for p in serial.tools.list_ports.comports()
                      if self._is_arduino_like(p)]
        if self.ser and self.ser.is_open:
            self.status_label.config(text=f"●  Connected: {self.port_var.get()}",
                                      foreground=C["OK_GREEN"])
        else:
            auto = self._find_arduino_port(infos)
            if auto:
                self.status_label.config(
                    text=f"○  Found: {self._port_label(auto)} — not connected",
                    foreground=C["ACCENT"])
            else:
                self.status_label.config(text="●  Disconnected", foreground=C["ERR_RED"])

    def _toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None
            self.connect_btn.config(text="Connect")
            self._refresh_ports()
        else:
            label = self.port_var.get()
            if not label or label not in self.port_map:
                messagebox.showerror("No port selected",
                                      "No Arduino-compatible device is connected.")
                return
            port = self.port_map[label]
            try:
                self.ser = serial.Serial(port, 9600, timeout=1)
                time.sleep(2)
                self.connect_btn.config(text="Disconnect")
                threading.Thread(target=self._listen_serial, daemon=True).start()
                self._refresh_status_label()
            except Exception as e:
                messagebox.showerror("Connection failed", str(e))

    def _listen_serial(self):
        while self.ser and self.ser.is_open:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    self._log(line)
            except Exception:
                break

    def _log(self, msg):
        self.log_indicator.config(text=msg)

    def _send_line(self, line):
        if not self.ser or not self.ser.is_open:
            messagebox.showerror("Not connected", "Connect to the Arduino first.")
            return False
        self.ser.write((line + "\n").encode())
        return True

    # ============================================================= ACTIONS

    def _send_protocol(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showerror("Not connected", "Connect to the Arduino first.")
            return

        self._check_pin_conflicts()
        pins = [ch.get_pin_int() for ch in self.channels]
        if any(pins.count(p) > 1 for p in pins):
            messagebox.showerror("Pin conflict",
                                  "Two or more LEDs are assigned to the same pin. "
                                  "Fix the conflicting pin assignments before sending.")
            return

        for i, ch in enumerate(self.channels):
            self._send_line(f"PIN,{i+1},{ch.get_pin_int()}")
            time.sleep(0.05)

        for i, ch in enumerate(self.channels):
            enabled = 1 if ch.enabled.get() else 0
            intensity = ch.get_flash_intensity_int()
            on_ms, dark_ms, continuous = ch.get_pulse_timing()
            if continuous:
                mode, on_t, off_t = 0, 0, 0
            else:
                mode  = 1
                on_t  = on_ms / 1000.0
                off_t = dark_ms / 1000.0
            self._send_line(f"CFG,{i+1},{enabled},{intensity},{mode},{on_t},{off_t}")
            time.sleep(0.05)

        use_global = 1 if self.use_global_duration.get() else 0
        global_secs = self._get_global_duration_secs()
        self._send_line(f"DUR,{use_global},{global_secs}")
        time.sleep(0.05)

        if not self.use_global_duration.get():
            for i, ch in enumerate(self.channels):
                self._send_line(f"CHDUR,{i+1},{ch.duration.get() * 60}")
                time.sleep(0.05)

        delay_en = 1 if self.delay_enabled.get() else 0
        self._send_line(f"DELAY,{delay_en},{self._get_delay_secs()}")
        self._log("--- Protocol sent ---")

    def _start_experiment(self):
        self._send_line("RUN")
        self._log("--- Experiment started ---")

    def _stop_experiment(self):
        self._send_line("STOP")
        self._log("--- Stopped ---")

    # ======================================================== SAVE / LOAD

    def _save_protocol(self):
        suggested = self.protocol_name.get().strip() or "protocol"
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=f"{suggested}.json",
            filetypes=[("BOLT protocol", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self.protocol_name.set(os.path.splitext(os.path.basename(path))[0])
        data = {
            "protocol_name":       self.protocol_name.get(),
            "protocol_notes":      self.protocol_notes.get(),
            "channels":            [ch.to_dict() for ch in self.channels],
            "use_global_duration": self.use_global_duration.get(),
            "global_dur_h":        self._safe_int(self.global_dur_h),
            "global_dur_m":        self._safe_int(self.global_dur_m),
            "global_dur_s":        self._safe_int(self.global_dur_s),
            "delay_enabled":       self.delay_enabled.get(),
            "delay_h":             self._safe_int(self.delay_h),
            "delay_m":             self._safe_int(self.delay_m),
            "delay_s":             self._safe_int(self.delay_s),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self._remember_recent(path)
        self._log(f"Saved: {os.path.basename(path)}")

    def _load_protocol(self):
        path = filedialog.askopenfilename(
            filetypes=[("BOLT protocol", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self._load_protocol_from_path(path)

    def _load_protocol_from_path(self, path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        self.protocol_name.set(data.get("protocol_name", ""))
        self.protocol_notes.set(data.get("protocol_notes", ""))
        for ch, d in zip(self.channels, data.get("channels", [])):
            ch.from_dict(d)
        self.use_global_duration.set(data.get("use_global_duration", True))
        self.global_dur_h.set(data.get("global_dur_h", 0))
        self.global_dur_m.set(data.get("global_dur_m", 0))
        self.global_dur_s.set(data.get("global_dur_s", 0))
        self.delay_enabled.set(data.get("delay_enabled", False))
        self.delay_h.set(data.get("delay_h", 0))
        self.delay_m.set(data.get("delay_m", 0))
        self.delay_s.set(data.get("delay_s", 0))
        for i in range(NUM_CHANNELS):
            self._refresh_pulse_widgets(i)
        self._on_global_duration_toggle()
        self._check_pin_conflicts()
        self._update_schematic()
        self._update_duty_graph()
        self._remember_recent(path)
        self._log(f"Loaded: {os.path.basename(path)}")

    # ======================================================== RECENT FILES

    def _load_recent(self):
        try:
            with open(RECENT_PATH, "r") as f:
                paths = json.load(f)
            return [p for p in paths if os.path.isfile(p)][:RECENT_MAX]
        except Exception:
            return []

    def _save_recent(self):
        try:
            with open(RECENT_PATH, "w") as f:
                json.dump(self.recent_paths, f)
        except Exception:
            pass

    def _remember_recent(self, path):
        path = os.path.abspath(path)
        self.recent_paths = [path] + [p for p in self.recent_paths if p != path]
        self.recent_paths = self.recent_paths[:RECENT_MAX]
        self._save_recent()
        self._refresh_recent_combo()

    def _refresh_recent_combo(self):
        labels = [os.path.basename(p) for p in self.recent_paths]
        self.recent_combo["values"] = labels

    def _on_recent_selected(self, _event=None):
        label = self.recent_var.get()
        for p in self.recent_paths:
            if os.path.basename(p) == label:
                self._load_protocol_from_path(p)
                break
        self.recent_var.set("Select…")


def main():
    root = tk.Tk()
    app  = BoltGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
