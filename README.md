# backbone

One visual language for the back* suite, implemented once. Colours, margins,
box drawing, meters, prompt widgets and the live-view loop that backtrack and
backcrack both draw themselves with, so a list in one looks and behaves like a
list in the other.

Python, stdlib only - no third-party packages at all. Requires Python 3.9+.

This is a library, not a tool. It has no entry points of its own; something
else imports it.

## Install

Not on PyPI. It's installed from this checkout, editable:

    pip3 install -e .

backtrack and backcrack each declare it as a `file:` dependency, so installing
either one pulls it in automatically and the first install wins - pip sees the
requirement already satisfied for the second. Editable means edits here take
effect in both immediately, no reinstall.

## What's in it

**`ui`** - the visual layer. `Colors` (honours `NO_COLOR=1`), the global
`MARGIN_H` / `MARGIN_V` inset every frame is drawn inside, `rule`, `bar`,
`header_box`, `sparkline`, `spinner`, and the `SPIN` / `PARTS` / `SPARK` glyph
sets. Also the ANSI-aware text measuring (`visual_len`, `truncate_text`,
`clip_ansi`) that makes any of that survive colour codes and wide characters.

**`prompt`** - the widgets. `select` (single or multi), `confirm`, `text`,
`path`, `live_select`, `list_edit`, plus the specialised editors backtrack's
tag work needs: `calendar_select`, `datetime_edit`, `time_edit`,
`fraction_edit`, `number_edit`, `rating_edit`, `equaliser_edit`, `rva2_edit`,
`system_editor_edit`. All resize-aware, all mouse-aware, all rendered through
the same painter.

**`prompt_core`** - the primitives underneath: the screen-diff painter, the
`Choice` and `Column` types, footer-hint click mapping, and `run_dashboard`.

**`nav`** - `NAV_STACK`, the app-wide breadcrumb, and `QuitToTerminal`, which
derives from `BaseException` specifically so an editor's `except Exception`
can't swallow a quit.

**`datetime_parse`** - one date and time parser for everything that reads a
hand-typed date, keeping whatever precision was given and saying why when it
can't read something.

## Live views

`run_dashboard(render, interval=..., on_key=...)` drives a tick-driven view
through the same `_Widget` machinery every prompt widget uses, rather than each
tool hand-rolling a redraw loop:

    from backbone.prompt_core import run_dashboard

    def render() -> list:
        return ["  line one", "  line two"]

    run_dashboard(render, interval=1.0, quit_key="q")

`render()` returns the whole frame as a list of lines, each carrying its own
left indent, and only runs once per `interval`. Keypresses and resizes are
checked every 50ms regardless, so a resize repaints and a `q` quits at once
instead of waiting out the data-refresh cadence. `on_key` handles anything
other than the quit key and is free to open a `select()` or `confirm()` of its
own; the dashboard repaints from scratch when it returns.

Piped or redirected output falls back to a plain sleep loop and never reads
keys, so a view left running in a log behaves.

## Terminal size

Everything reads `get_terminal_width()`, one cached value invalidated by
SIGWINCH, rather than calling out to the OS per line. `consume_resize()` tells
a loop a resize has landed since it last asked, which is the signal to clear
and repaint rather than diff.

`content_width()` is that width minus the margins, with no artificial floor:
content is always sized against the real terminal, however narrow, so a hard
clip later can't cut an oversized frame apart mid-border.

## Status bar and background work

`set_status(task_id, message)` registers running background work, which shows
in the status line as a pulsing beacon until the id is cleared. `show_status`
is the transient one-line toast. `set_footer_provider(fn)` registers a
persistent box drawn above the status line, which is how backtrack's
now-playing bar works; register nothing and no box is drawn.

## Self-checks

Two modules check their own pure logic, no terminal needed:

    python3 -m backbone.ui
    python3 -m backbone.prompt_core

Both print an OK line. They cover the parts a terminal-less run can actually
exercise: sizing, measuring, truncation, table widths, hint parsing. Raw mode,
key reading and screen painting need a real tty and are not covered.
