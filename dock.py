"""TABS — every workspace opens as a tab inside the main window instead of its own window
(Ryan, 2026-10-17: "can we update the app so the different makes dont open new windows but
are more like tabs inside the one window").

The workspaces were all written as Toplevels (`win = tk.Toplevel(root)`, then win.title(),
win.geometry(), win.protocol("WM_DELETE_WINDOW", ...), a Close button calling win.destroy()).
Instead of rewriting each one, `window(root)` hands back a DockedWindow: a Frame that answers
the Toplevel calls those windows make —
    title()      -> the tab's label
    lift() / deiconify() / focus_force() -> select this tab
    protocol("WM_DELETE_WINDOW", fn) -> what the tab's x does
    destroy()    -> the tab closes, the previous one shows
    geometry / minsize / resizable / transient / attributes -> ignored (the tab fills the area)

⚠️ Bindings. A Frame is NOT in its children's bindtags (only their toplevel is), so a key or
wheel binding made on the window would never fire. DockedWindow.bind() routes those
sequences through the dock, which binds them ONCE on the root window and calls the SELECTED
tab's handlers (scoped_wheel's canvas scrolling, a window's Escape key). Structure events
(<Configure>, <Destroy>, <Map>) bind on the frame as usual.

No dock installed (drivers that build a window alone, or tabs turned off in settings):
window() returns a plain tk.Toplevel, exactly as before.
"""
import tkinter as tk

_DOCK = {"d": None}

# sequences that come from the widgets INSIDE the window, so they need routing
_ROUTED = ("Key", "Escape", "Return", "KP_Enter", "Tab", "BackSpace", "Delete", "MouseWheel",
           "TouchpadScroll", "Button-4", "Button-5", "Command-", "Control-", "Mod1-", "Meta-",
           "Alt-", "Option-", "Up>", "Down>", "Left>", "Right>", "space", "Prior", "Next",
           "Home>", "End>")


def _routed(seq):
    s = str(seq or "")
    if any(k in s for k in ("Configure", "Destroy", "Map", "Unmap", "Visibility", "Expose",
                            "FocusIn", "FocusOut", "Enter", "Leave", "Motion",
                            "ButtonPress", "ButtonRelease", "Button-1", "Button-2",
                            "Button-3", "B1-", "Double-")):
        return False
    return any(k in s for k in _ROUTED) or (s.startswith("<") and len(s) == 3) \
        or (len(s) == 1)


class DockedWindow(tk.Frame):
    """A tab that behaves enough like a Toplevel for the workspace code."""

    def __init__(self, dock, **kw):
        tk.Frame.__init__(self, dock.area, **kw)
        self._dock = dock
        self._title = "Workspace"
        self._on_close = None
        self._keys = {}                 # sequence -> [func]
        dock._add(self)
        self.after_idle(self._hide_close)

    def _hide_close(self):
        """A tab has its own x: the window's own header "Close" button is a second way
        to do the same thing (round-2 review, new user), so it's hidden in a tab."""
        try:
            for fr in self.winfo_children()[:2]:
                for w in fr.winfo_children():
                    if isinstance(w, tk.Button) and str(w.cget("text")) == "Close":
                        w.pack_forget()
        except tk.TclError:
            pass

    # ---- the Toplevel API the workspaces use
    def title(self, text=None):
        if text is None:
            return self._title
        self._title = str(text)
        self._dock._paint()

    wm_title = title

    def protocol(self, name=None, func=None):
        if name == "WM_DELETE_WINDOW" and func is not None:
            self._on_close = func

    wm_protocol = protocol

    def geometry(self, *a, **k):
        return "%dx%d+0+0" % (self.winfo_width(), self.winfo_height())

    wm_geometry = geometry

    def _noop(self, *a, **k):
        return ""

    minsize = maxsize = resizable = transient = attributes = wm_attributes = _noop
    iconify = overrideredirect = group = iconname = iconphoto = wm_minsize = _noop
    wm_maxsize = wm_resizable = wm_transient = _noop

    def state(self, *a):
        return "normal"

    wm_state = state

    def withdraw(self):
        self._dock.hide(self)

    def deiconify(self):
        self._dock.select(self)

    def lift(self, *a):
        self._dock.select(self)

    tkraise = lift

    def focus_force(self):
        self._dock.select(self)
        try:
            self.focus_set()
        except tk.TclError:
            pass

    def destroy(self):
        d = self._dock
        try:
            d._remove(self)
        finally:
            tk.Frame.destroy(self)

    def close(self):
        """The tab's x: what the window's own close does (it may ask, or clean up)."""
        if self._on_close is not None:
            self._on_close()
        else:
            self.destroy()

    # ---- bindings: key / wheel sequences go through the dock (see the module docstring)
    def bind(self, sequence=None, func=None, add=None):
        if sequence is not None and func is not None and _routed(sequence):
            lst = self._keys.setdefault(sequence, [])
            if not add:
                del lst[:]
            lst.append(func)
            self._dock._route(sequence)
            return sequence
        return tk.Frame.bind(self, sequence, func, add)

    def unbind(self, sequence, funcid=None):
        if _routed(sequence):
            self._keys.pop(sequence, None)
            return
        return tk.Frame.unbind(self, sequence, funcid)


class Dock:
    """The tab strip + the area the tabs fill. `home` is the main form (Image / Video)."""

    def __init__(self, root, strip, area, home, C, font, on_change=None):
        self.root, self.strip, self.area, self.home = root, strip, area, home
        self.C, self.font, self.on_change = C, font, on_change
        self.tabs = []                  # DockedWindow, in the order they were opened
        self.cur = None                 # None = home
        self.history = []
        self.routed = set()
        home.place(in_=area, relx=0, rely=0, relwidth=1, relheight=1)
        self._paint()

    def window(self, **kw):
        return DockedWindow(self, **kw)

    def _add(self, w):
        self.tabs.append(w)
        self.select(w)

    def _remove(self, w):
        if w in self.tabs:
            self.tabs.remove(w)
        self.history = [h for h in self.history if h is not w]
        if not self._alive():
            return
        if self.cur is w:
            nxt = self.history.pop() if self.history else None
            self.select(nxt)                    # (cur is still w here, so it switches)
            self.history = [h for h in self.history if h is not w]
        else:
            self._paint()

    def select(self, w):
        """Show tab `w` (None = the home form)."""
        if w is not None and w not in self.tabs:
            w = None
        if self.cur is not None and self.cur not in self.tabs:
            self.cur = False                    # the shown tab is closing: always switch
        if self.cur is not w:
            if self.cur is not None or w is not None:
                self.history = [h for h in self.history if h is not self.cur] + (
                    [self.cur] if self.cur is not False else [])
            for t in self.tabs:
                if t is not w:
                    t.place_forget()
            if w is None:
                self.home.place(in_=self.area, relx=0, rely=0, relwidth=1, relheight=1)
            else:
                self.home.place_forget()
                w.place(in_=self.area, relx=0, rely=0, relwidth=1, relheight=1)
            self.cur = w
        self._paint()
        if self.on_change:
            try:
                self.on_change(w)
            except Exception:
                pass

    def hide(self, w):
        if self.cur is w:
            self.select(None)

    def _label(self, w):
        # The first tab is the MAKE tab (Image · Video). ⚠️ Do not relabel it "Home":
        # Ryan (2026-10-22) asked for a NEW home tab, and a renamed Make tab is not that.
        t = w.title() if w is not None else "Image \u00b7 Video"
        for pre in ("Forge Lab — ", "Forge Lab - ", "Forge Lab: ", "Forge Lab · "):
            if t.startswith(pre):
                t = t[len(pre):]
        return (t[:26] + "…") if len(t) > 27 else t

    def _alive(self):
        try:
            return bool(self.strip.winfo_exists())
        except tk.TclError:
            return False

    def _paint(self):
        if not self._alive():           # the app is closing
            return
        C = self.C
        for c in self.strip.winfo_children():
            c.destroy()
        for w in [None] + self.tabs:
            on = w is self.cur
            bg = C["panel2"] if on else C["bg"]
            tab = tk.Frame(self.strip, bg=bg, cursor="hand2")
            tab.pack(side="left", padx=(0, 2))
            lb = tk.Label(tab, text=self._label(w), bg=bg, fg=C["ink"] if on else C["muted"],
                          font=self.font, padx=10, pady=5, cursor="hand2")
            lb.pack(side="left")
            for x in (tab, lb):
                x.bind("<Button-1>", lambda _e, w_=w: self.select(w_))
            if w is not None:
                xb = tk.Label(tab, text="×", bg=bg, fg=C["muted"], font=self.font,
                              padx=6, cursor="hand2")
                xb.pack(side="left")
                xb.bind("<Button-1>", lambda _e, w_=w: w_.close())
            if on:
                tk.Frame(tab, bg=C["ochre"], height=2).place(relx=0, rely=1.0, relwidth=1,
                                                             anchor="sw")

    # ---- routed bindings
    def _route(self, seq):
        if seq in self.routed:
            return
        self.routed.add(seq)
        self.root.bind(seq, lambda e, s=seq: self._dispatch(s, e), add="+")

    def _dispatch(self, seq, ev):
        w = self.cur
        if w is None:
            return None
        try:
            if not str(ev.widget).startswith(str(w)):
                return None             # the event isn't inside the shown tab
        except Exception:
            return None
        out = None
        for fn in list(w._keys.get(seq) or []):
            r = fn(ev)
            if r == "break":
                out = "break"
        return out


def install(dock):
    _DOCK["d"] = dock


def installed():
    return _DOCK["d"]


def window(root, **kw):
    """A workspace window: a tab when the dock is installed, else a Toplevel (as before)."""
    d = _DOCK["d"]
    if d is not None:
        try:
            if d.root.winfo_exists():
                return d.window(**kw)
        except tk.TclError:
            pass
    return tk.Toplevel(root, **kw)
