# backbone

Everything the back* tools share, written once: colours, margins, box
drawing, meters, prompt widgets and a live-view loop, so a list in one tool
looks and behaves like a list in another, plus the plumbing underneath (logging,
dates, CLI output, atomic files, background processes, notifications). backtrack
and backcrack both build on it, and a new tool should too: anything that isn't
specific to one tool's job belongs here.

Python, stdlib only - no third-party packages at all. Requires Python 3.10+.

This is a library, not a tool. It has no entry points of its own; something
else imports it.

## Install

On PyPI as `backpack-backbone` (the import name is still `backbone`).
backtrack and backcrack depend on it, so installing either one pulls it in;
there's no reason to install it on its own unless you're building a tool.

To work on it, install this checkout editable before the tools, so edits here
take effect immediately with no reinstall:

    pip3 install -e .

pip then sees the requirement already satisfied and leaves it.

`backbone.deps` is how a tool says what it needs besides Python: each tool's
`doctor` command reports it, and `deps.require` stops a tool at startup with
how to install anything it can't run without.

## What's in it

**`ui`** - the visual layer. `Colors` (honours `NO_COLOR=1`), the global
`MARGIN_H` / `MARGIN_V` inset every frame is drawn inside, `wrap_margins`,
`rule`, `bar`, `header_box`, `sparkline`, `spinner`, `rate_of_change`, and
the `SPIN` / `PARTS` / `SPARK` glyph sets. Also the ANSI-aware text
measuring (`visual_len`, `truncate_text`, `clip_ansi`, `strip_ansi`) that
makes any of that survive colour codes and wide characters, and small
formatters: `plural`, `human_gb`, `dir_size_kb`. The accent colour is chosen
with `set_accent` (an `ACCENT_PRESETS` key or `#RRGGBB`); a tool calls it once
at startup from its own settings, and `accent_code` / `accent_label` let a
settings screen check and name a value.

`from backbone import ...` exposes only a subset of `ui` (`Colors`, the
margins and glyph sets, `spinner`, `content_width`, `rule`, `bar`,
`header_box`, `wrap_margins`); import anything else from its module, e.g.
`from backbone.ui import human_gb`.

**`prompt`** - the widgets. `select` (single or multi), `confirm`, `text`,
`path`, `live_select`, `list_edit`, plus specialised editors for tag-style
values: `calendar_select`, `datetime_edit`, `time_edit`,
`fraction_edit`, `number_edit`, `rating_edit`, `equaliser_edit`, `rva2_edit`,
`system_editor_edit`. All resize-aware, all mouse-aware, all rendered through
the same painter.

**`prompt.core`** - the primitives underneath: the screen-diff painter, key
reading, the `Choice` and `Column` types, the footer hint bar (`hint`) and its
click mapping, and `run_dashboard`.

**`nav`** - `NAV_STACK`, the app-wide breadcrumb, and `QuitToTerminal`, which
derives from `BaseException` specifically so an editor's `except Exception`
can't swallow a quit.

**`datetime_parse`** - one date and time parser for everything that reads a
hand-typed date, keeping whatever precision was given and saying why when it
can't read something.

**`app`** - who is running. A tool calls `app.configure("name", config_dir)`
once at startup (from its config module); the log file and the hints toggle
are kept in that folder, named after the tool.

**`log`** - the diagnostics log, `<config_dir>/<name>.log`, off until the
tool calls `log.configure(True)`. `from backbone.log import log` to write to
it, and `with quietly():` to carry on past an error on purpose while still
logging it.

**`output`** - a CLI's one output path: `table`, `record`, `event`, `note`,
`fail`, drawn as human text on a terminal or JSON / NDJSON with `--json`, and
the exit codes that go with them.

**`files`** - `write_text_atomic`, `backup_copy`, `log_line` (a daemon's
timestamped line, printed and appended to its log), `disk_free`,
`count_entries`.

**`procs`** - a tool's own background processes: `spawn_module` starts one (`python -m`)
detached, `find_processes` / `stop_processes` find or SIGTERM them by name
however they were started (directly, through `python3`, or through a launcher
command given as `launcher=`), `ps_listing` for the raw process table.

**`notify`** - `ntfy(server, topic, title, message)` pushes to a phone in the
background, doing nothing without a topic; `chime()` plays a sound at the
machine.

**`timefmt`**, **`numbering`**, **`keyboard`**, **`terminal_input`** - clock
and SRT timestamps; arabic, roman or written-out numbers; the keyboard layout
family (for typo scoring); raw key reads and escape decoding.

**`prompt.timezone`** - a full-screen world-map timezone picker.

## Live views

`run_dashboard(render, interval=..., on_key=...)` drives a tick-driven view
through the same `_Widget` machinery every prompt widget uses, rather than each
tool hand-rolling a redraw loop:

    from backbone.prompt.core import run_dashboard

    def render() -> list:
        return ["  line one", "  line two"]

    run_dashboard(render, interval=1.0, quit_key="q")

`render()` returns the whole frame as a list of lines, each carrying its own
left indent, and only runs once per `interval`. Keypresses and resizes are
checked every 50ms regardless, so a resize repaints and a `q` quits at once
instead of waiting out the data-refresh cadence. `on_key` handles anything
other than the quit key and is free to open a `select()` or `confirm()` of its
own; the dashboard repaints from scratch when it returns.

When stdin isn't a terminal (run from a script, or with input redirected),
it falls back to a plain sleep loop and never reads keys, so such a view has
to be stopped from outside.

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
persistent box drawn above the status line, such as backtrack's now-playing
bar; register nothing and no box is drawn. `prompt` also has optional hooks
for a host app's playback keys and player view (`set_transport_handler`,
`set_player_opener`), unused unless registered.

## Self-checks

Two modules check their own pure logic, no terminal needed:

    python3 -m backbone.ui
    python3 -m backbone.prompt.core

Both print an OK line. They cover the parts that run without a terminal:
sizing, measuring, truncation, table widths, hint parsing. Raw mode,
key reading and screen painting need a real tty and are not covered.

The tests in `tests/` each run on their own:

    for f in tests/test_*.py; do python3 "$f"; done
