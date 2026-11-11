"""Local presentation components. No device access or motion state changes."""
import sys
import tkinter as tk
from math import sqrt
from tkinter import ttk

BG = "#EFEDF0"
SURFACE = "#FFFCFF"
INPUT = "#EFEDF1"
TEXT = "#22252D"
MUTED = "#626B7B"
LINE = "#E6E4ED"
BLUE = "#2477F5"
FONT = "Microsoft YaHei UI"

MIN_CLIENT_WIDTH, MIN_CLIENT_HEIGHT = 1100, 760


def _desktop_area(root):
    """Work rectangle and total frame size, in the window's coordinate units."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        monitor_at = user32.MonitorFromPoint
        monitor_at.argtypes = (wintypes.POINT, wintypes.DWORD)
        monitor_at.restype = wintypes.HANDLE
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        monitor = monitor_at(wintypes.POINT(*root.winfo_pointerxy()), 2)  # nearest monitor
        if user32.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info)):
            work = info.work
            frame = wintypes.RECT()
            # Standard resizable, decorated Tk toplevel (WS_OVERLAPPEDWINDOW).
            if user32.AdjustWindowRectEx(ctypes.byref(frame), 0x00CF0000, False, 0):
                return (work.left, work.top, work.right, work.bottom,
                        frame.right - frame.left, frame.bottom - frame.top)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), 16, 40


def configure_startup_window(root):
    left, top, right, bottom, frame_w, frame_h = _desktop_area(root)
    available_w = max(1, right - left - frame_w)
    available_h = max(1, bottom - top - frame_h)
    width = min(available_w, 1600, max(MIN_CLIENT_WIDTH, round(available_w * 0.90)))
    height = min(available_h, 1000, max(MIN_CLIENT_HEIGHT, round(available_h * 0.92)))
    minimum = min(width, MIN_CLIENT_WIDTH), min(height, MIN_CLIENT_HEIGHT)
    root.minsize(*minimum)
    x = left + (available_w - width) // 2
    y = top + (available_h - height) // 2
    # Keep the '+' prefix for absolute coordinates on monitors left of/above primary.
    root.geometry(f"{width}x{height}+{x}+{y}")
    return minimum != (MIN_CLIENT_WIDTH, MIN_CLIENT_HEIGHT)


def scrollable_work_area(parent, min_width, min_height):
    """Keep the notebook reachable on small displays while the header stays fixed."""
    viewport = ttk.Frame(parent)
    viewport.rowconfigure(0, weight=1)
    viewport.columnconfigure(0, weight=1)
    canvas = tk.Canvas(viewport, borderwidth=0, highlightthickness=0, background=BG)
    canvas.grid(row=0, column=0, sticky="nsew")
    horizontal = ttk.Scrollbar(viewport, orient="horizontal", command=canvas.xview)
    vertical = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
    content = ttk.Frame(canvas, style="App.TFrame")
    item = canvas.create_window(0, 0, anchor="nw", window=content)

    def fit(event):
        width, height = event.width, event.height
        needs_x = width < min_width
        needs_y = height - (horizontal.winfo_reqheight() if needs_x else 0) < min_height
        if needs_y:
            vertical.grid(row=0, column=1, sticky="ns")
            width -= vertical.winfo_reqwidth()
        else:
            vertical.grid_remove()
        if width < min_width:
            horizontal.grid(row=1, column=0, sticky="ew")
            height -= horizontal.winfo_reqheight()
        else:
            horizontal.grid_remove()
        width, height = max(min_width, width), max(min_height, height)
        canvas.itemconfigure(item, width=width, height=height)
        canvas.configure(scrollregion=(0, 0, width, height))

    viewport.bind("<Configure>", fit)
    return viewport, content


def rounded_button(style, root, name, normal, hover, disabled=INPUT):
    """Nine-slice native ttk element, with the standard focus and key bindings."""
    images = []
    for color in (normal, hover, disabled):
        photo = tk.PhotoImage(master=root, width=32, height=32)
        for y in range(32):
            dy = max(0, 11.5 - y, y - 19.5)
            inset = round(12 - sqrt(max(0, 144 - dy * dy)))
            photo.put(color, to=(inset, y, 32 - inset, y + 1))
        images.append(photo)
    root._theme_images.extend(images)
    element = name + ".rounded"
    style.element_create(element, "image", images[0], ("disabled", images[2]),
                         ("pressed", images[1]), ("active", images[1]), border=13, padding=0, sticky="nsew")
    style.configure(name, background=SURFACE)
    style.map(name, background=[])
    style.layout(name, [(element, {"sticky": "nsew", "children": [
        ("Button.padding", {"sticky": "nsew", "children": [
            ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])


def apply_theme(root):
    root.configure(background=BG)
    style = ttk.Style(root)
    root._theme_images = []
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), background=SURFACE, foreground=TEXT)
    for name, color in (("TFrame", SURFACE), ("App.TFrame", BG),
                        ("CardAlt.TFrame", INPUT), ("Header.TFrame", BG),
                        ("HeaderMetric.TFrame", BG)):
        style.configure(name, background=color)
    style.configure("TLabel", background=SURFACE, foreground=TEXT)
    for name, bg, fg, size, weight in (
        ("Muted.TLabel", SURFACE, MUTED, 9, "normal"),
        ("CardMuted.TLabel", INPUT, MUTED, 9, "normal"),
        ("CardValue.TLabel", INPUT, TEXT, 10, "bold"),
        ("Value.TLabel", SURFACE, TEXT, 10, "bold"),
        ("Section.TLabel", SURFACE, TEXT, 12, "bold"),
        ("HeaderTitle.TLabel", BG, TEXT, 20, "bold"),
        ("HeaderSubtitle.TLabel", BG, MUTED, 9, "normal"),
        ("HeaderMetricLabel.TLabel", BG, MUTED, 9, "normal"),
    ):
        style.configure(name, background=bg, foreground=fg, font=(FONT, size, weight))
    for name, bg, fg in (
        ("ReadonlyField", INPUT, TEXT), ("StepBadge", "#E5EFFF", "#155DCC"),
        ("BadgeReady", "#E8F4E9", "#286341"), ("BadgeWarn", "#FFF0D9", "#855014"),
        ("BadgeMuted", INPUT, MUTED), ("Status", "#E5EFFF", "#155DCC"),
        ("StatusReady", "#E8F4E9", "#286341"), ("StatusWarning", "#FFF0D9", "#855014"),
        ("HeaderMetricValue", INPUT, TEXT), ("HeaderMetricConnected", "#E8F4E9", "#286341"),
        ("HeaderMetricDisconnected", INPUT, MUTED), ("HeaderMetricPos", "#E5EFFF", "#155DCC"),
    ):
        style.configure(name + ".TLabel", background=bg, foreground=fg,
                        padding=(8, 3), font=(FONT, 9, "bold"))
    style.configure("TLabelframe", background=SURFACE, bordercolor=LINE, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=TEXT,
                    font=(FONT, 11, "bold"), padding=(6, 4))
    style.configure("TButton", background=INPUT, foreground=TEXT, bordercolor=INPUT,
                    lightcolor=INPUT, darkcolor=INPUT, relief="flat", borderwidth=0,
                    padding=(10, 4), font=(FONT, 10), focusthickness=2, focuscolor=BLUE)
    style.map("TButton", background=[("disabled", INPUT), ("pressed", "#D6DCE7"), ("active", "#E2E6EE")],
              foreground=[("disabled", "#777D88")])
    rounded_button(style, root, "TButton", INPUT, "#E2E6EE")
    for name, bg, fg, hover in (
        ("Accent", BLUE, "white", "#155DCC"), ("Start", BLUE, "white", "#155DCC"),
        ("Good", "#E5EFFF", "#155DCC", "#D8E7FF"),
        ("Setup", "#E5EFFF", "#155DCC", "#D8E7FF"),
        ("Danger", "#C73545", "white", "#A92233"), ("Stop", "#C73545", "white", "#A92233"),
        ("Shortcut", INPUT, TEXT, "#E2E6EE"),
        ("ShortcutSelected", BLUE, "white", "#155DCC"),
        ("ShortcutActive", "#286341", "white", "#205435"),
    ):
        style.configure(name + ".TButton", background=bg, foreground=fg, bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, padding=(10, 4), font=(FONT, 10, "bold"),
                        focuscolor="white" if fg == "white" else BLUE)
        style.map(name + ".TButton", background=[("disabled", INPUT), ("pressed", hover), ("active", hover)],
                  foreground=[("disabled", "#777D88")])
        rounded_button(style, root, name + ".TButton", bg, hover,
                       bg if name in ("ShortcutSelected", "ShortcutActive") else INPUT)
        if name in ("ShortcutSelected", "ShortcutActive"):
            style.map(name + ".TButton", foreground=[("disabled", "white")])
    style.configure("Start.TButton", font=(FONT, 12, "bold"), padding=(12, 5))
    style.configure("Shortcut.TButton", font=(FONT, 9), padding=(8, 4))
    for name in ("ShortcutSelected", "ShortcutActive"):
        style.configure(name + ".TButton", font=(FONT, 9, "bold"))
    for name in ("TEntry", "TCombobox"):
        style.configure(name, fieldbackground=INPUT, foreground=TEXT, background=INPUT,
                        bordercolor=LINE, lightcolor=INPUT, darkcolor=INPUT, padding=(6, 4), arrowcolor=MUTED)
        style.map(name, bordercolor=[("focus", BLUE)],
                  fieldbackground=[("disabled", INPUT), ("readonly", INPUT), ("focus", "white")],
                  foreground=[("disabled", "#777D88")])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=SURFACE, foreground=TEXT, padding=(4, 5))
        style.map(name, background=[("active", SURFACE)], foreground=[("disabled", "#777D88")])
    # Retain a keyboard-visible focus ring in segmented speed controls.
    style.layout("Speed.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
        ("Radiobutton.focus", {"sticky": "nswe", "children": [("Radiobutton.label", {"sticky": "nswe"})]})]})])
    style.configure("Speed.TRadiobutton", background=INPUT, foreground=MUTED, anchor="center",
                    padding=(8, 4), font=(FONT, 9), focuscolor=BLUE)
    style.map("Speed.TRadiobutton", background=[("selected", BLUE), ("active", "#E2E6EE")],
              foreground=[("disabled", "#777D88"), ("selected", "white")])
    style.configure("CompactTitle.TLabel", background=BG, foreground=TEXT, font=(FONT, 17, "bold"))
    style.layout("Nav.TRadiobutton", style.layout("Speed.TRadiobutton"))
    style.configure("Nav.TRadiobutton", background=BG, foreground=MUTED,
                    padding=(8, 4), font=(FONT, 10, "bold"), focuscolor=BLUE)
    style.map("Nav.TRadiobutton", background=[("selected", "#E5EFFF"), ("active", INPUT)],
              foreground=[("selected", "#155DCC")])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 10))
    style.layout("TNotebook", [("Notebook.padding", {"sticky": "nsew"})])
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 10), font=(FONT, 10, "bold"))
    style.layout("TNotebook.Tab", [("Notebook.padding", {"sticky": "nsew", "children": [
        ("Notebook.focus", {"sticky": "nsew", "children": [("Notebook.label", {"sticky": "nsew"})]})]})])
    style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", INPUT)],
              foreground=[("selected", "#155DCC")], padding=[], expand=[])
    style.layout("Main.TNotebook.Tab", [])
    style.configure("Main.TNotebook", tabmargins=0)
    style.configure("Horizontal.TScale", background=BLUE, troughcolor=INPUT, bordercolor=INPUT, sliderlength=24)
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor=INPUT, bordercolor=INPUT)
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                    rowheight=32, borderwidth=0, font=(FONT, 10))
    style.configure("Treeview.Heading", background=INPUT, foreground=TEXT, font=(FONT, 10, "bold"), padding=8)
    style.map("Treeview", background=[("selected", BLUE)], foreground=[("selected", "white")])
    style.configure("TScrollbar", background="#CFCDD6", troughcolor=BG, borderwidth=0, arrowsize=12)


class RoundedCard(tk.Frame):
    """Native content in a rounded surface; canvas never intercepts controls."""
    def __init__(self, parent, title):
        super().__init__(parent, background=BG, borderwidth=0)
        self.backdrop = tk.Canvas(self, background=BG, highlightthickness=0, borderwidth=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        self.heading = ttk.Label(self.body, text=title, style="Section.TLabel")
        self.heading.pack(anchor="w", pady=(0, 6))
        self.content = ttk.Frame(self.body)
        self.content.pack(fill="both", expand=True)
        self.bind("<Configure>", self._paint)

    def _paint(self, event):
        c = self.backdrop
        c.delete("all")
        def rounded(x, y, w, h, radius, fill):
            points = [x+radius,y, w-radius,y, w,y, w,y+radius, w,h-radius,
                      w,h, w-radius,h, x+radius,h, x,h, x,h-radius, x,y+radius, x,y]
            c.create_polygon(points, smooth=True, splinesteps=24, fill=fill, outline="")
        rounded(1, 3, event.width-1, event.height, 18, "#E7E4EA")
        rounded(1, 1, event.width-1, event.height-3, 18, SURFACE)
