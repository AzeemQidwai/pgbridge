"""Custom enterprise widgets.

Tk's stock controls read as 1998 no matter how they are styled, so the pieces
that carry the interface — buttons, meters, the bridge header, and pipeline rails —
are drawn on canvases with modern enterprise styling, subtle lighting, and
deliberate micro-interactions. Everything is keyboard reachable and respects
disabled state.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .theme import C, RADIUS


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    """Draw a smooth anti-aliased rounded rectangle."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=28, **kwargs)


# ---------------------------------------------------------------------------
class Button(tk.Canvas):
    """Modern enterprise canvas button with tactile feedback, subtle bevel lighting,
    and crisp focus states."""

    VARIANTS = {
        "primary": (C["source"], C["on_accent"], C["source_light"]),
        "target":  (C["target"], "#FFFFFF", C["target_light"]),
        "danger":  (C["stop"], "#FFFFFF", "#F87171"),
        "ghost":   (C["raised"], C["text"], C["line"]),
        "quiet":   (None, C["muted"], None),
    }

    def __init__(self, master, text: str, command: Callable[[], None] | None = None,
                 variant: str = "ghost", font=None, width: int | None = None,
                 height: int = 34, background: str | None = None, icon: str = "", **kw):
        self.bg_host = background or _host_bg(master)
        super().__init__(master, height=height, highlightthickness=0,
                         background=self.bg_host, bd=0, **kw)
        self.text = text
        self.icon = icon
        self.command = command
        self.variant = variant
        self.font = font or ("TkDefaultFont", 10)
        self._enabled = True
        self._hover = False
        self._press = False
        self._height = height

        self._pad = 18 if variant not in ("quiet",) else 10
        self._fixed_width = width
        self._fit()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Key-Return>", lambda _e: self._fire())
        self.bind("<Key-space>", lambda _e: self._fire())
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self.configure(takefocus=True, cursor="hand2")
        self._draw()

    def _measure(self, text: str) -> int:
        try:
            return self.font.measure(text)
        except AttributeError:
            return len(text) * 8

    def _fit(self):
        full_text = f"{self.icon} {self.text}".strip() if self.icon else self.text
        self.configure(width=max(self._fixed_width or 0,
                                 self._measure(full_text) + self._pad * 2, 84))

    def set_text(self, text: str):
        self.text = text
        self._fit()
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=enabled)
        self._draw()

    # -- events ------------------------------------------------------------
    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _e):
        self._hover = self._press = False
        self._draw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._press = True
        self.focus_set()
        self._draw()

    def _on_release(self, _e):
        was = self._press
        self._press = False
        self._draw()
        if was and self._enabled:
            self._fire()

    def _fire(self):
        if self._enabled and self.command:
            self.command()

    # -- paint -------------------------------------------------------------
    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or int(self["width"])
        h = self._height
        fill, fg, border = self.VARIANTS.get(self.variant, self.VARIANTS["ghost"])

        if not self._enabled:
            fill = C["raised"] if self.variant != "quiet" else None
            fg = C["faint"]
            border = C["line"] if border else None
        elif self._press:
            fill = _shade(fill, -0.15) if fill else C["hover"]
            border = _shade(border, -0.15) if border else None
        elif self._hover:
            fill = _shade(fill, 0.12) if fill else C["hover"]
            border = C["border_active"] if border else None

        # Draw container body
        if fill or border:
            round_rect(self, 1, 1, w - 1, h - 1, RADIUS,
                       fill=fill or self.bg_host,
                       outline=border or (fill or self.bg_host), width=1)
            # Subtle top-edge light reflection for physical depth on solid buttons
            if fill and self._enabled and not self._press and self.variant in ("primary", "target", "danger"):
                self.create_line(RADIUS + 2, 2, w - RADIUS - 2, 2,
                                 fill=_shade(fill, 0.25), width=1)

        # Focus ring
        if self.focus_get() is self and self._enabled:
            round_rect(self, 2, 2, w - 2, h - 2, RADIUS,
                       fill="", outline=C["source"], width=1)

        display_text = f"{self.icon} {self.text}".strip() if self.icon else self.text
        self.create_text(w / 2, h / 2 + (1 if not self._press else 2),
                         text=display_text, fill=fg, font=self.font, anchor="center")


# ---------------------------------------------------------------------------
class Pill(tk.Canvas):
    """Modern capsule status badge with glowing status dot and high-contrast typography."""

    def __init__(self, master, text: str = "", tone: str = "muted", font=None,
                 background: str | None = None, **kw):
        self.bg_host = background or _host_bg(master)
        super().__init__(master, height=24, highlightthickness=0, bd=0,
                         background=self.bg_host, **kw)
        self.font = font or ("TkDefaultFont", 9)
        self.text = text
        self.tone = tone
        self._draw()

    def set(self, text: str, tone: str = "muted"):
        self.text = text
        self.tone = tone
        self._draw()

    def _draw(self):
        self.delete("all")
        colour = C.get(self.tone, C["muted"])
        try:
            tw = self.font.measure(self.text)
        except AttributeError:
            tw = len(self.text) * 7
        w = max(tw + 34, 48)
        self.configure(width=w)

        # Translucent pill fill with crisp tinted outline
        fill_bg = _mix(colour, self.bg_host, 0.88)
        border_col = _mix(colour, self.bg_host, 0.50)
        round_rect(self, 1, 1, w - 1, 23, 11,
                   fill=fill_bg, outline=border_col, width=1)

        # Saturated glowing status dot with halo
        self.create_oval(9, 9, 15, 15, fill=colour, outline="")
        self.create_oval(8, 8, 16, 16, outline=_mix(colour, fill_bg, 0.5), width=1)

        # Label text
        self.create_text(22, 12, text=self.text, anchor="w",
                         fill=C["text"] if self.tone in ("stop", "warn", "ok") else _shade(colour, 0.25),
                         font=self.font)


# ---------------------------------------------------------------------------
class Meter(tk.Canvas):
    """Modern rounded progress bar with gradient styling and smooth indeterminate sweep."""

    def __init__(self, master, height: int = 8, width: int = 40, tone: str = "source",
                 background: str | None = None, **kw):
        self.bg_host = background or _host_bg(master)
        super().__init__(master, height=height, width=width, highlightthickness=0, bd=0,
                         background=self.bg_host, **kw)
        self._value = 0.0
        self._tone = tone
        self._h = height
        self._sweep = None
        self._offset = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, fraction: float, tone: str | None = None):
        self.stop()
        self._value = max(0.0, min(1.0, fraction))
        if tone:
            self._tone = tone
        self._draw()

    def start(self):
        if self._sweep is None:
            self._tick()

    def stop(self):
        if self._sweep is not None:
            self.after_cancel(self._sweep)
            self._sweep = None

    def _tick(self):
        self._offset = (self._offset + 0.025) % 1.0
        self._draw(indeterminate=True)
        self._sweep = self.after(25, self._tick)

    def _draw(self, indeterminate: bool = False):
        self.delete("all")
        w = self.winfo_width() or 200
        h = self._h
        colour = C.get(self._tone, C["source"])
        r = h / 2

        # Background track with subtle border
        round_rect(self, 0, 0, w, h, r, fill=C["raised"], outline=C["line"], width=1)

        if indeterminate:
            span = w * 0.30
            x = -span + (w + span) * self._offset
            x_start = max(0, x)
            x_end = min(w, x + span)
            if x_end > x_start:
                round_rect(self, x_start, 0, x_end, h, r,
                           fill=colour, outline="")
                # High-tech pulse bead
                mid_x = (x_start + x_end) / 2
                self.create_oval(mid_x - 3, 1, mid_x + 3, h - 1,
                                 fill=_mix(colour, "#FFFFFF", 0.5), outline="")
        elif self._value > 0:
            fill_w = max(h, w * self._value)
            round_rect(self, 0, 0, fill_w, h, r,
                       fill=colour, outline="")
            # Subtle edge gleam
            if fill_w > h:
                self.create_line(1, 1, fill_w - 2, 1, fill=_shade(colour, 0.25), width=1)


# ---------------------------------------------------------------------------
class BridgeHeader(tk.Canvas):
    """The enterprise persistent header: PostgreSQL and SQL Server endpoints,
    brand identity, and dynamic animated data flow conduit."""

    def __init__(self, master, fonts: dict, height: int = 98, **kw):
        super().__init__(master, height=height, highlightthickness=0, bd=0,
                         background=C["bg"], **kw)
        self.fonts = fonts
        self._h = height
        self.source = {"label": "Source not connected", "sub": "PostgreSQL",
                       "live": False}
        self.target = {"label": "Target not connected", "sub": "SQL Server",
                       "live": False}
        self._flowing = False
        self._phase = 0.0
        self._job = None
        self._caption = ""
        self.bind("<Configure>", lambda _e: self._draw())

    def set_endpoint(self, side: str, label: str, sub: str, live: bool):
        getattr(self, side).update(label=label, sub=sub, live=live)
        self._draw()

    def set_caption(self, text: str):
        self._caption = text
        self._draw()

    def set_flowing(self, flowing: bool):
        if flowing == self._flowing:
            return
        self._flowing = flowing
        if flowing:
            self._animate()
        elif self._job is not None:
            self.after_cancel(self._job)
            self._job = None
            self._draw()

    def _animate(self):
        self._phase = (self._phase + 0.022) % 1.0
        self._draw()
        self._job = self.after(28, self._animate)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 1000
        h = self._h
        mid = h / 2

        # Symmetrical node dimensions
        node_w = min(320, max(210, (w - 280) / 2))

        # Brand header badge at top
        self._draw_brand(w)

        # Source & Target Endpoint Cards
        self._node(16, mid - 2, node_w, self.source, C["source"], "PostgreSQL", "SOURCE")
        self._node(w - 16 - node_w, mid - 2, node_w, self.target, C["target"], "SQL Server", "TARGET")

        # The Pipeline Conduit
        x1 = 16 + node_w + 14
        x2 = w - 16 - node_w - 14
        if x2 > x1:
            # Conduit housing pipe
            conduit_mid = mid - 2
            round_rect(self, x1, conduit_mid - 4, x2, conduit_mid + 4, 4,
                       fill=C["raised"], outline=C["line"], width=1)

            if self._flowing:
                span = x2 - x1
                # Multi-particle energy stream
                for i in range(6):
                    p = ((self._phase + i / 6.0) % 1.0)
                    px = x1 + span * p
                    particle_col = _mix(C["source"], C["target"], p)
                    # Outer halo
                    self.create_oval(px - 5, conduit_mid - 5, px + 5, conduit_mid + 5,
                                     fill="", outline=_mix(particle_col, C["bg"], 0.4), width=1)
                    # Core bead
                    self.create_oval(px - 3, conduit_mid - 3, px + 3, conduit_mid + 3,
                                     fill=particle_col, outline="")
                    # Center hot spot
                    self.create_oval(px - 1, conduit_mid - 1, px + 1, conduit_mid + 1,
                                     fill="#FFFFFF", outline="")
            else:
                # Directional chevrons along conduit
                span = x2 - x1
                step = max(36, span / 5)
                cur = x1 + step * 0.8
                while cur < x2 - 10:
                    self.create_line(cur - 4, conduit_mid - 3, cur, conduit_mid,
                                     cur - 4, conduit_mid + 3,
                                     fill=C["line"], width=1.5)
                    cur += step

            # Telemetry / Caption Chip
            caption_text = self._caption if self._caption else ("TRANSFER IN PROGRESS" if self._flowing else "POSTGRESQL → SQL SERVER")
            chip_col = C["source"] if self._flowing else C["muted"]
            try:
                cw = self.fonts["small"].measure(caption_text) + 24
            except AttributeError:
                cw = len(caption_text) * 7 + 24
            cx = (x1 + x2) / 2
            round_rect(self, cx - cw / 2, conduit_mid + 14, cx + cw / 2, conduit_mid + 34, 10,
                       fill=_mix(chip_col, C["bg"], 0.88),
                       outline=_mix(chip_col, C["bg"], 0.50), width=1)
            self.create_text(cx, conduit_mid + 24, text=caption_text,
                             fill=chip_col if self._flowing else C["faint"],
                             font=self.fonts["small"], anchor="center")

    def _draw_brand(self, w: int):
        # Subtle bottom separation line across header
        self.create_line(16, self._h - 1, w - 16, self._h - 1, fill=C["line"], width=1)

    def _node(self, x, cy, w, data, colour, default_badge, role):
        top, bot = cy - 32, cy + 32

        # Card container with clean border
        border_col = _mix(colour, C["line"], 0.6) if data["live"] else C["line"]
        round_rect(self, x, top, x + w, bot, RADIUS + 3,
                   fill=C["panel"], outline=border_col, width=1)

        # Subtle top accent bar
        self.create_line(x + RADIUS, top + 1, x + w - RADIUS, top + 1,
                         fill=colour if data["live"] else C["line"], width=2)

        # Status halo & pip
        dot_x, dot_y = x + 20, cy
        if data["live"]:
            # Concentric glowing pulse rings
            self.create_oval(dot_x - 10, dot_y - 10, dot_x + 10, dot_y + 10,
                             outline=_mix(colour, C["panel"], 0.4), width=1)
            self.create_oval(dot_x - 6, dot_y - 6, dot_x + 6, dot_y + 6,
                             fill=colour, outline="")
            self.create_oval(dot_x - 2, dot_y - 2, dot_x + 2, dot_y + 2,
                             fill="#FFFFFF", outline="")
        else:
            self.create_oval(dot_x - 5, dot_y - 5, dot_x + 5, dot_y + 5,
                             fill=C["faint"], outline="")

        # Engine Tag & Role
        badge_text = data["sub"] or default_badge
        self.create_text(x + 38, cy - 14, text=f"{role} • {badge_text}", anchor="w",
                         fill=colour if data["live"] else C["muted"],
                         font=self.fonts["badge"])

        # Database / Host Label
        label = data["label"]
        max_w = w - 50
        while label and self.fonts["strong"].measure(label) > max_w and len(label) > 4:
            label = label[:-2] + "\u2026"
        self.create_text(x + 38, cy + 8, text=label, anchor="w",
                         fill=C["text"], font=self.fonts["strong"])


# ---------------------------------------------------------------------------
class StageRail(ttk.Frame):
    """Linear-style navigation sidebar with uppercase tracking header,
    emerald checkmark status badges, and luminous active indicators."""

    def __init__(self, master, stages: list[tuple[str, str]],
                 on_select: Callable[[int], None], fonts: dict, **kw):
        super().__init__(master, style="Rail.TFrame", **kw)
        self.fonts = fonts
        self.on_select = on_select
        self.items: list[tk.Canvas] = []
        self.enabled: list[bool] = []
        self.active = 0
        self.state: list[str] = ["idle"] * len(stages)
        self._hover_index: int | None = None

        # Sidebar tracking header
        head = tk.Canvas(self, height=36, highlightthickness=0, bd=0, background=C["panel"])
        head.pack(fill="x", pady=(4, 6))
        head.create_text(16, 18, text="MIGRATION PIPELINE", anchor="w",
                         fill=C["faint"], font=self.fonts["badge"])
        head.create_line(16, 35, 190, 35, fill=C["line"], width=1)

        for index, (title, hint) in enumerate(stages):
            row = tk.Canvas(self, height=54, highlightthickness=0, bd=0,
                            background=C["panel"], cursor="hand2")
            row.pack(fill="x", pady=1)
            row.bind("<Button-1>", lambda _e, i=index: self._click(i))
            row.bind("<Configure>", lambda _e, i=index: self._paint(i))
            row.bind("<Enter>", lambda _e, i=index: self._enter(i))
            row.bind("<Leave>", lambda _e, i=index: self._leave(i))
            row.title, row.hint = title, hint
            self.items.append(row)
            self.enabled.append(index == 0)

        self.visible = list(range(len(stages)))

    def _enter(self, index: int):
        if self.enabled[index]:
            self._hover_index = index
            self._paint(index)

    def _leave(self, index: int):
        if self._hover_index == index:
            self._hover_index = None
            self._paint(index)

    def show(self, indices: list[int]):
        self.visible = [i for i in range(len(self.items)) if i in indices]
        for row in self.items:
            row.pack_forget()
        for i in self.visible:
            self.items[i].pack(fill="x", pady=1)
            self._paint(i)

    def _click(self, index: int):
        if self.enabled[index]:
            self.on_select(index)

    def unlock(self, index: int):
        if 0 <= index < len(self.enabled):
            self.enabled[index] = True
            self._paint(index)

    def set_state(self, index: int, state: str):
        self.state[index] = state
        self._paint(index)

    def select(self, index: int):
        self.active = index
        for i in range(len(self.items)):
            self._paint(i)

    def _paint(self, index: int):
        row = self.items[index]
        row.delete("all")
        w = row.winfo_width() or 206
        active = index == self.active
        on = self.enabled[index]
        state = self.state[index]
        hover = (self._hover_index == index) and on and not active

        # Background surface
        if active:
            round_rect(row, 6, 2, w - 6, 52, RADIUS, fill=C["raised"],
                       outline=C["border_active"], width=1)
            # Glowing accent pill on left
            round_rect(row, 8, 12, 12, 42, 2, fill=C["source"], outline="")
        elif hover:
            round_rect(row, 6, 2, w - 6, 52, RADIUS, fill=C["hover"],
                       outline=C["line"], width=1)

        # Status Pip / Numeral
        number = self.visible.index(index) + 1 if index in self.visible else index + 1
        num_str = f"0{number}" if number < 10 else f"{number}"
        cx, cy = 28, 27

        if state == "done":
            # Emerald checkmark badge
            row.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                            fill=_mix(C["ok"], C["panel"], 0.85),
                            outline=C["ok"], width=1)
            row.create_text(cx, cy, text="✓", fill=C["ok"],
                            font=self.fonts["strong"], anchor="center")
        elif state == "fail":
            # Coral cross badge
            row.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                            fill=_mix(C["stop"], C["panel"], 0.85),
                            outline=C["stop"], width=1)
            row.create_text(cx, cy, text="✕", fill=C["stop"],
                            font=self.fonts["strong"], anchor="center")
        elif state == "busy":
            # Amber pulse badge
            row.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                            fill=_mix(C["warn"], C["panel"], 0.85),
                            outline=C["warn"], width=1)
            row.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=C["warn"], outline="")
        elif active:
            # Active step circle
            row.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                            fill=_mix(C["source"], C["panel"], 0.85),
                            outline=C["source"], width=1)
            row.create_text(cx, cy, text=num_str, fill=C["source"],
                            font=self.fonts["badge"], anchor="center")
        else:
            # Clean numeral
            fg_col = C["muted"] if on else C["faint"]
            row.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                            fill=C["panel"], outline=C["line"] if on else C["bg"], width=1)
            row.create_text(cx, cy, text=num_str, fill=fg_col,
                            font=self.fonts["badge"], anchor="center")

        # Titles
        title_fg = C["text"] if active else (C["text"] if on else C["faint"])
        row.create_text(48, 19, text=row.title, anchor="w", fill=title_fg,
                         font=self.fonts["strong" if active else "body"])
        row.create_text(48, 36, text=row.hint, anchor="w",
                         fill=C["muted"] if active else C["faint"],
                         font=self.fonts["small"])


# ---------------------------------------------------------------------------
class LogView(ttk.Frame):
    """Append-only activity log with syntax-highlighted severity tags."""

    TONES = {"info": C["muted"], "ok": C["ok"], "warn": C["warn"],
             "error": C["stop"], "step": C["text"]}

    def __init__(self, master, fonts: dict, height: int = 9, **kw):
        super().__init__(master, style="Panel.TFrame", **kw)
        self.text = tk.Text(self, height=height, wrap="word", bd=0,
                            background=C["panel"], foreground=C["muted"],
                            insertbackground=C["text"], font=fonts["data_sm"],
                            padx=14, pady=10, highlightthickness=0,
                            selectbackground=C["hover"], state="disabled")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for name, colour in self.TONES.items():
            self.text.tag_configure(name, foreground=colour)
        self.text.tag_configure("time", foreground=C["faint"])
        self.autoscroll = True

    def write(self, message: str, level: str = "info", stamp: str = ""):
        self.text.configure(state="normal")
        if stamp:
            self.text.insert("end", stamp + "  ", "time")
        self.text.insert("end", message + "\n", level if level in self.TONES else "info")
        if self.autoscroll:
            self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def dump(self) -> str:
        return self.text.get("1.0", "end")


# ---------------------------------------------------------------------------
class ScrollArea(ttk.Frame):
    """A vertically scrollable region with auto-hiding scrollbar."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                background=_host_bg(master))
        self.bar = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body,
                                                 anchor="nw")
        self.body.bind("<Configure>", lambda _e: self._resize())
        self.canvas.bind("<Configure>", self._fit_width)
        self.bind("<Enter>", lambda _e: self._wheel(True))
        self.bind("<Leave>", lambda _e: self._wheel(False))
        self._bar_shown = False

    def _fit_width(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._resize()

    def _resize(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height() + 1
        if needed and not self._bar_shown:
            self.bar.pack(side="right", fill="y")
            self._bar_shown = True
        elif not needed and self._bar_shown:
            self.bar.pack_forget()
            self._bar_shown = False
            self.canvas.yview_moveto(0)

    def _wheel(self, on: bool):
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            if on:
                self.bind_all(sequence, self._scroll, add="+")
            else:
                self.unbind_all(sequence)

    def _scroll(self, event):
        if not self._bar_shown:
            return
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


# ---------------------------------------------------------------------------
class SearchableCombobox(ttk.Combobox):
    """A dropdown with a separate search query; filtering never changes selection."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._popup = None
        self._outside_binding = None
        self.bind("<Button-1>", self._click_search)
        self.bind("<Down>", self._open_search)
        self.bind("<Alt-Down>", self._open_search)
        self.bind("<Destroy>", lambda _e: self._close_search())

    def _click_search(self, event):
        if "downarrow" in self.identify(event.x, event.y) or self.instate(["readonly"]):
            return self._open_search()

    def _open_search(self, _event=None):
        if self.instate(["disabled"]):
            return "break"
        if self._popup is not None:
            self._close_search()
            return "break"
        popup = self._popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.transient(self.winfo_toplevel())
        popup.configure(background=C["line"], padx=1, pady=1)
        body = ttk.Frame(popup, style="Panel.TFrame", padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Search by name", style="PanelMuted.TLabel").pack(anchor="w", pady=(0, 6))
        self._query = tk.StringVar(popup)
        self._search_entry = ttk.Entry(body, textvariable=self._query)
        self._search_entry.pack(fill="x", pady=(0, 8))
        results = ttk.Frame(body, style="Panel.TFrame")
        results.pack(fill="both", expand=True)
        self._matches = tk.Listbox(results, height=8, exportselection=False,
                                  background=C["panel"], foreground=C["text"],
                                  selectbackground=C["hover"], selectforeground=C["text"],
                                  font=self.cget("font"), relief="flat", borderwidth=0,
                                  highlightthickness=0, activestyle="none")
        scrollbar = ttk.Scrollbar(results, command=self._matches.yview)
        self._matches.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._matches.pack(side="left", fill="both", expand=True)
        self._match_note = ttk.Label(body, style="PanelMuted.TLabel")
        self._match_note.pack(anchor="w", pady=(8, 0))
        self._query.trace_add("write", self._filter_search)
        self._filter_search()
        self._search_entry.bind("<Down>", self._focus_matches)
        self._search_entry.bind("<Return>", self._choose_search)
        self._matches.bind("<Return>", self._choose_search)
        self._matches.bind("<ButtonRelease-1>", self._choose_search)
        popup.bind("<Escape>", lambda _e: self._close_search(focus=True))
        popup.bind("<FocusOut>", lambda _e: self.after_idle(self._close_if_outside))
        popup.update_idletasks()
        width = max(self.winfo_width(), 280)
        height = popup.winfo_reqheight()
        x = min(self.winfo_rootx(), max(0, self.winfo_screenwidth() - width))
        y = self.winfo_rooty() + self.winfo_height()
        if y + height > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - height)
        popup.geometry(f"{width}x{height}+{max(0, x)}+{y}")
        self._outside_binding = self.winfo_toplevel().bind(
            "<Button-1>", self._outside_click, add="+")
        popup.deiconify()
        self._search_entry.focus_set()
        return "break"

    def _filter_search(self, *_args):
        term = self._query.get().casefold().strip()
        values = self.cget("values")
        matches = [value for value in values if term in value.casefold()]
        self._matches.delete(0, "end")
        for value in matches:
            self._matches.insert("end", value)
        if matches:
            self._matches.selection_set(0)
            self._matches.activate(0)
        self._match_note.configure(text=f"{len(matches)} of {len(values)} matches" if matches else "No matching names")

    def _focus_matches(self, _event=None):
        if self._matches.size():
            self._matches.focus_set()
        return "break"

    def _choose_search(self, _event=None):
        selected = self._matches.curselection()
        if selected:
            value = self._matches.get(selected[0])
            self._close_search(focus=True)
            self.set(value)
            self.event_generate("<<ComboboxSelected>>")
        return "break"

    def _outside_click(self, event):
        if event.widget is not self:
            self._close_search()

    def _close_if_outside(self):
        if self._popup is not None:
            focused = self.focus_get()
            if focused is None or focused.winfo_toplevel() != self._popup:
                self._close_search()

    def _close_search(self, focus=False):
        if self._outside_binding:
            self.winfo_toplevel().unbind("<Button-1>", self._outside_binding)
            self._outside_binding = None
        if self._popup is not None:
            popup, self._popup = self._popup, None
            popup.destroy()
            if focus:
                self.focus_set()
        return "break"


class Field(ttk.Frame):
    """Labelled input field with modern spacing and clean typography."""

    def __init__(self, master, label: str, fonts: dict, kind: str = "entry",
                 values: list[str] | None = None, secret: bool = False, searchable: bool = False,
                 width: int = 24, style_prefix: str = "Panel", **kw):
        super().__init__(master, style=f"{style_prefix}.TFrame", **kw)
        ttk.Label(self, text=label, style=f"{style_prefix}Muted.TLabel"
                  if style_prefix == "Panel" else "Muted.TLabel").pack(
                      anchor="w", pady=(0, 5))
        self.secret = secret
        self.var = tk.StringVar()
        if kind == "combo":
            combo_class = SearchableCombobox if searchable else ttk.Combobox
            self.widget = combo_class(self, textvariable=self.var,
                                       values=values or [], width=width,
                                       state="readonly")
        elif kind == "spin":
            self.widget = ttk.Spinbox(self, textvariable=self.var, width=width,
                                      from_=1, to=1_000_000, increment=1000)
        else:
            self.widget = ttk.Entry(self, textvariable=self.var, width=width,
                                    show="\u2022" if secret else "")
        self.widget.pack(fill="x")

    def get(self) -> str:
        return self.var.get() if self.secret else self.var.get().strip()

    def set(self, value):
        self.var.set("" if value is None else str(value))

    def set_values(self, values: list[str]):
        self.widget.configure(values=values)


# ---------------------------------------------------------------------------
class MetricTile(ttk.Frame):
    """Modern KPI metric card displaying title, big bold value, and subtle status."""

    def __init__(self, master, title: str, value: str = "—", fonts: dict | None = None,
                 tone: str = "source", **kw):
        super().__init__(master, style="Raised.TFrame", padding=(14, 10), **kw)
        fonts = fonts or {}
        lbl_font = fonts.get("badge", ("TkDefaultFont", 8, "bold"))
        val_font = fonts.get("kpi_num", ("TkDefaultFont", 16, "bold"))

        ttk.Label(self, text=title.upper(), style="PanelMuted.TLabel",
                  font=lbl_font).pack(anchor="w")
        self.val_lbl = ttk.Label(self, text=value, background=C["raised"],
                                 foreground=C["text"], font=val_font)
        self.val_lbl.pack(anchor="w", pady=(2, 0))

    def set(self, value: str):
        self.val_lbl.configure(text=str(value))


# ---------------------------------------------------------------------------
def section(master, title: str, fonts: dict, subtitle: str = "") -> ttk.Frame:
    """Titled section container with modern typography and clean separation."""
    wrap = ttk.Frame(master)
    head = ttk.Frame(wrap)
    head.pack(fill="x")
    ttk.Label(head, text=title, style="Title.TLabel").pack(anchor="w")
    if subtitle:
        sub = ttk.Label(head, text=subtitle, style="Muted.TLabel",
                        justify="left")
        sub.pack(anchor="w", pady=(4, 0), fill="x")
        head.bind("<Configure>",
                  lambda e, l=sub: l.configure(wraplength=max(e.width - 8, 220)))
    ttk.Separator(wrap, orient="horizontal").pack(fill="x", pady=(12, 16))
    body = ttk.Frame(wrap)
    body.pack(fill="both", expand=True)
    wrap.body = body
    return wrap


def card_frame(master, padding=(18, 16), **kw) -> ttk.Frame:
    """Create a modern elevated card container."""
    return ttk.Frame(master, style="Panel.TFrame", padding=padding, **kw)


def _host_bg(widget) -> str:
    try:
        return widget.cget("background") or C["bg"]
    except tk.TclError:
        try:
            style = ttk.Style()
            return style.lookup(widget.cget("style") or "TFrame", "background") or C["bg"]
        except tk.TclError:
            return C["bg"]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(v))) for v in rgb)


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _hex_to_rgb(a), _hex_to_rgb(b)
    return _rgb_to_hex([ra[i] + (rb[i] - ra[i]) * t for i in range(3)])


def _shade(colour: str | None, amount: float) -> str | None:
    if colour is None:
        return None
    target = "#FFFFFF" if amount > 0 else "#000000"
    return _mix(colour, target, abs(amount))
