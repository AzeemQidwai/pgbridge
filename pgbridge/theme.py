"""Design tokens and ttk styling.

Two system hues carry the whole interface — teal for the source, indigo for the
target — because every screen is about the relationship between two databases.
Semantic colours stay separate from both so a warning never reads as a system.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

C = {
    "bg":            "#0C111B",  # Deep obsidian root canvas
    "panel":         "#151D2B",  # Primary elevated surface (sidebars, card backgrounds)
    "raised":        "#1C2433",  # Secondary elevated surface (nested panels, inputs, tables)
    "hover":         "#253146",  # Interactive hover surface
    "card":          "#151C28",  # Card surface container
    "line":          "#232D3F",  # Subtle 1px structural borders
    "border_active": "#3B4D6B",  # Active / focused border
    "text":          "#F8FAFC",  # Crisp pure white primary text
    "muted":         "#94A3B8",  # Slate-400 secondary / label text
    "faint":         "#8B9AB0",  # Slate-500 tertiary / caption text
    "source":        "#55D6CA",  # Electric Cyan (PostgreSQL source)
    "source_light":  "#85E8DE",  # Light cyan highlight
    "source_dark":   "#083344",  # Translucent cyan container tint
    "target":        "#6366F1",  # Radiant Indigo (SQL Server target)
    "target_light":  "#818CF8",  # Light indigo highlight
    "target_dark":   "#1E1B4B",  # Translucent indigo container tint
    "ok":            "#10B981",  # Vibrant Emerald (success / verified)
    "ok_dark":       "#064E3B",  # Dark emerald tint
    "warn":          "#F59E0B",  # Saturated Amber (warning / in-flight)
    "warn_dark":     "#451A03",  # Dark amber tint
    "stop":          "#EF4444",  # Crisp Coral Red (error / blocker)
    "stop_dark":     "#450A0A",  # Dark red tint
    "on_accent":     "#050811",  # High-contrast text on bright accent fills
    "accent_glow":   "#38BDF8",  # Accent glow highlight
}

SP = {"xs": 4, "sm": 8, "md": 14, "lg": 22, "xl": 34}
RADIUS = 8


def resolve_fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
    available = set(tkfont.families(root))

    def pick(candidates: list[str], fallback: str) -> str:
        for name in candidates:
            if name in available:
                return name
        return fallback

    ui = pick(["Ubuntu Sans", "Noto Sans", "Segoe UI Variable Text", "Segoe UI",
               "SF Pro Text", "Helvetica Neue", "Ubuntu", "Lato",
               "Liberation Sans", "DejaVu Sans"], "TkDefaultFont")
    mono = pick(["Ubuntu Sans Mono", "Noto Sans Mono", "Cascadia Mono",
                 "JetBrains Mono", "SF Mono", "Menlo", "Consolas",
                 "Ubuntu Mono", "Liberation Mono", "DejaVu Sans Mono"], "TkFixedFont")

    return {
        "display": tkfont.Font(family=ui, size=18, weight="bold"),
        "title":   tkfont.Font(family=ui, size=13, weight="bold"),
        "body":    tkfont.Font(family=ui, size=10),
        "strong":  tkfont.Font(family=ui, size=10, weight="bold"),
        "small":   tkfont.Font(family=ui, size=9),
        "badge":   tkfont.Font(family=ui, size=8, weight="bold"),
        "data":    tkfont.Font(family=mono, size=10),
        "data_sm": tkfont.Font(family=mono, size=9),
        "kpi_num": tkfont.Font(family=ui, size=17, weight="bold"),
    }


def apply(root: tk.Misc) -> dict[str, tkfont.Font]:
    fonts = resolve_fonts(root)
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(background=C["bg"])

    style.configure(".", background=C["bg"], foreground=C["text"],
                    fieldbackground=C["raised"], bordercolor=C["line"],
                    lightcolor=C["line"], darkcolor=C["line"],
                    font=fonts["body"], focuscolor=C["source"])

    style.configure("TFrame", background=C["bg"])
    style.configure("Panel.TFrame", background=C["panel"])
    style.configure("Card.TFrame", background=C["card"])
    style.configure("Raised.TFrame", background=C["raised"])
    style.configure("Rail.TFrame", background=C["panel"])

    style.configure("TLabel", background=C["bg"], foreground=C["text"])
    style.configure("Panel.TLabel", background=C["panel"], foreground=C["text"])
    style.configure("Card.TLabel", background=C["card"], foreground=C["text"])
    style.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"],
                    font=fonts["small"])
    style.configure("PanelMuted.TLabel", background=C["panel"],
                    foreground=C["muted"], font=fonts["small"])
    style.configure("CardMuted.TLabel", background=C["card"],
                    foreground=C["muted"], font=fonts["small"])
    style.configure("Title.TLabel", background=C["bg"], foreground=C["text"],
                    font=fonts["title"])
    style.configure("Display.TLabel", background=C["bg"], foreground=C["text"],
                    font=fonts["display"])
    style.configure("Data.TLabel", background=C["bg"], foreground=C["text"],
                    font=fonts["data"])

    # Entries and combos ----------------------------------------------------
    style.configure("TEntry", fieldbackground=C["raised"], foreground=C["text"],
                    insertcolor=C["text"], borderwidth=1, relief="flat",
                    padding=(10, 8))
    style.map("TEntry",
              bordercolor=[("focus", C["source"]), ("active", C["border_active"])],
              lightcolor=[("focus", C["source"]), ("active", C["border_active"])],
              darkcolor=[("focus", C["source"]), ("active", C["border_active"])])

    style.configure("TCombobox", fieldbackground=C["raised"], background=C["raised"],
                    foreground=C["text"], arrowcolor=C["muted"], borderwidth=1,
                    padding=(10, 7), relief="flat")
    style.map("TCombobox",
              fieldbackground=[("readonly", C["raised"])],
              bordercolor=[("focus", C["source"]), ("active", C["border_active"])],
              arrowcolor=[("hover", C["text"]), ("active", C["text"])])
    root.option_add("*TCombobox*Listbox.background", C["raised"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["source"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["on_accent"])
    root.option_add("*TCombobox*Listbox.borderWidth", 1)
    root.option_add("*TCombobox*Listbox.relief", "flat")

    # Checkbuttons and radios ----------------------------------------------
    for base in ("TCheckbutton", "TRadiobutton"):
        style.configure(base, background=C["bg"], foreground=C["text"],
                        focuscolor=C["bg"], indicatorcolor=C["raised"],
                        indicatormargin=(0, 0, 8, 0), padding=(0, 4))
        style.map(base,
                  background=[("active", C["bg"])],
                  indicatorcolor=[("selected", C["source"]),
                                  ("active", C["hover"])],
                  foreground=[("disabled", C["faint"])])
    style.configure("Panel.TCheckbutton", background=C["panel"])
    style.map("Panel.TCheckbutton", background=[("active", C["panel"])])
    style.configure("Card.TCheckbutton", background=C["card"])
    style.map("Card.TCheckbutton", background=[("active", C["card"])])

    # Treeview --------------------------------------------------------------
    style.configure("Treeview",
                    background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["text"], borderwidth=0, relief="flat",
                    rowheight=30, font=fonts["body"])
    style.map("Treeview",
              background=[("selected", C["hover"])],
              foreground=[("selected", C["text"])])
    style.configure("Treeview.Heading",
                    background=C["raised"], foreground=C["muted"],
                    font=fonts["small"], relief="flat", borderwidth=0,
                    padding=(12, 10))
    style.map("Treeview.Heading",
              background=[("active", C["hover"])],
              foreground=[("active", C["text"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # Scrollbars ------------------------------------------------------------
    style.configure("Vertical.TScrollbar", background=C["line"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["bg"], relief="flat", width=10)
    style.map("Vertical.TScrollbar", background=[("active", C["muted"])])
    style.configure("Horizontal.TScrollbar", background=C["line"],
                    troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["bg"], relief="flat", width=10)

    style.configure("TSeparator", background=C["line"])
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"],
                    padding=(18, 10), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", C["panel"])],
              foreground=[("selected", C["text"])])

    style.configure("TSpinbox", fieldbackground=C["raised"], foreground=C["text"],
                    arrowcolor=C["muted"], borderwidth=1, relief="flat",
                    padding=(9, 7))
    style.map("TSpinbox",
              bordercolor=[("focus", C["source"])])

    return fonts
