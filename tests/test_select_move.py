"""select()'s J/K row moves: the caller is asked, the row swaps on screen only
when it agrees, and a move never crosses a separator or the ends of the list. Also: a ListPlace
finds the same item after a re-sort, and greyed rows can't be ticked."""
import unittest
from unittest.mock import patch

from backbone import keys, prompt
from backbone.prompt import lists


def _run(keys, choices, on_move=None, index=0, **kw):
    """Drive select() with a scripted key sequence, returning its result."""
    feed = iter(keys)
    with patch.object(lists, '_read_key', lambda fd: next(feed)), \
         patch.object(lists, '_wait_for_keypress', lambda t: True), \
         patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
         patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget'), \
         patch.object(lists.sys.stdin, 'fileno', lambda: 0):
        return prompt.select("", choices=choices, on_move=on_move, index=index, **kw)


class SelectMoveTest(unittest.TestCase):
    def test_moves_follow_the_row_and_stop_at_a_separator(self):
        order = ['a', 'b', 'c']
        calls = []

        def move(v, d):
            calls.append((v, d))
            i = order.index(v)
            if not 0 <= i + d < len(order):
                return False
            order[i], order[i + d] = order[i + d], order[i]
            return True

        choices = [prompt.Choice(title=x, value=x) for x in order] + [prompt.separator(), 'z']
        # K twice moves 'a' to the bottom; a third K would cross the separator.
        res = _run([keys.of('list.move_down')[0]] * 3 + ['ENTER'], choices, move)
        self.assertEqual(order, ['b', 'c', 'a'])
        self.assertEqual(res, 'a')                      # the cursor followed it
        self.assertEqual(calls, [('a', 1), ('a', 1)])   # never asked past the separator
        res = _run([keys.of('list.move_up')[0], 'ENTER'], [prompt.Choice(title=x, value=x) for x in order],
                   lambda v, d: False, index=2)
        self.assertEqual(res, 'a')                      # refused: nothing moved

    def test_a_place_comes_back_to_the_same_item_after_a_resort(self):
        place = prompt.ListPlace()
        _run(['DOWN', 's'], ['a', 'b', 'c'], shortcuts={'s': '__sort__'}, place=place)
        self.assertEqual(place.value, 'b')
        self.assertEqual(_run(['ENTER'], ['c', 'b', 'a'], place=place), 'b')
        # The item has gone: the same position instead.
        self.assertEqual(_run(['ENTER'], ['a', 'c'], place=place), 'c')
        place.reset()
        self.assertEqual(_run(['ENTER'], ['x', 'y'], place=place), 'x')


    def test_greyed_rows_cannot_be_ticked(self):
        def rows():
            return [prompt.Choice(title='same', value='same', disabled=True),
                    prompt.Choice(title='new', value='new', checked=True),
                    prompt.Choice(title='kept', value='kept', disabled=True)]
        # The cursor starts on the changing row; space there unticks it; moving
        # down can't reach the greyed row, so a second space re-ticks the same one.
        self.assertEqual(_run(['SPACE', 'DOWN', 'SPACE', 'ENTER'], rows(), multi=True), ['new'])
        self.assertEqual(_run(['SPACE', 'ENTER'], rows(), multi=True), [])
        self.assertEqual(_run(['SPACE', 'a', 'ENTER'], rows(), multi=True), ['new'])   # select-all too


if __name__ == "__main__":
    unittest.main()


class ScrollTest(unittest.TestCase):
    """A section heading scrolls into view with its first row, so the top of a
    list never reads "1 above" with only a heading up there."""
    def test_heading_comes_back_with_its_first_row(self):
        items = lists._norm([prompt.separator("A"), "a1", "a2", "a3",
                             prompt.separator("B"), "b1", "b2", "b3", "b4"])
        self.assertEqual(lists._scroll(4, 1, 4, items), 0)   # back up to a1: heading A shows
        self.assertEqual(lists._scroll(0, 5, 4, items), 2)   # down to b1: b1 at the bottom
        self.assertEqual(lists._scroll(6, 5, 4, items), 4)   # up to b1: heading B shows too
        self.assertEqual(lists._scroll(0, 8, 4, items), 5)   # never past the end


class OptionsMenuTest(unittest.TestCase):
    """o lists what can be done with the row (↵, the row actions that apply to
    it, with or without a key) and O what can be done with the whole list;
    picking an entry does what its key does."""
    def test_row_options_list_only_what_applies_and_run_it(self):
        done = []
        keys.define("t_opts", "Test", [("keyed", ("x",), "keyed action"),
                                       ("unkeyed", (), "unkeyed action"),
                                       ("only_b", (), "only on b")])
        try:
            shown = []
            real_menu = lists.options_menu
            def spy(title, entries):
                shown.append([e[0] for e in entries])
                return real_menu(title, entries)
            with patch.object(lists, 'options_menu', spy):
                # o on "a", then ↓↓ ↵ picks the third entry: the unkeyed action
                res = _run(['o', 'DOWN', 'DOWN', 'ENTER', 'ESC'], ['a', 'b'], choose_label="Play",
                           row_actions={"t_opts.keyed": lambda v: done.append(("keyed", v)),
                                        "t_opts.unkeyed": lambda v: done.append(("unkeyed", v)),
                                        "t_opts.only_b": lambda v: done.append(("only_b", v))},
                           row_action_applies=lambda spec, v: spec != "t_opts.only_b" or v == 'b')
            self.assertIsNone(res)
            self.assertEqual(shown, [["Play", "Keyed action", "Unkeyed action"]])
            self.assertEqual(done, [("unkeyed", 'a')])
        finally:
            for a in keys.actions("t_opts"):
                del keys._actions[a.id]

    def test_list_options_run_list_actions_and_shortcuts(self):
        done = []
        res = _run(['O', 'DOWN', 'ENTER', 'O', 'ENTER'], ['a', 'b'],
                   shortcuts={'s': '__sort__'}, extra_hints={'s': 'sort'},
                   list_actions={'z': lambda: done.append('all')}, list_action_hints={'z': 'add all'})
        self.assertEqual(done, ['all'])           # the second entry, a list action
        self.assertEqual(res, '__sort__')         # then the first: the shortcut's result


class SectionsTest(unittest.TestCase):
    """A sectioned list taller than the screen opens as its section titles: ↵
    opens one (and the next call comes back to it), Esc goes back to the
    titles, / switches to the whole list; a short one stays as it is."""
    def setUp(self):
        lists._section_memo.clear()
        self.choices = [prompt.separator("A"), "a1", "a2", "a3", prompt.separator("B"), "b1", "b2"]

    def run_(self, keys_, rows, **kw):
        with patch.object(lists, '_visible_rows', lambda: rows):
            return _run(keys_, self.choices, **kw)

    def test_short_list_is_left_whole(self):
        self.assertEqual(self.run_(['DOWN', 'ENTER'], 40), 'a2')

    def test_sections_then_a_section(self):
        self.assertEqual(self.run_(['DOWN', 'ENTER', 'DOWN', 'ENTER'], 4), 'b2')      # B, then b2
        # called again (a screen redrawing after a change): straight back into B, on b2
        self.assertEqual(self.run_(['ENTER'], 4, index=6), 'b2')
        # Esc leaves B for the titles; Esc there leaves the list
        self.assertIsNone(self.run_(['ESC', 'ESC'], 4, index=6))

    def test_slash_switches_to_the_whole_list_and_back(self):
        self.assertEqual(self.run_(['/', 'DOWN', 'ENTER'], 4), 'a2')                 # whole list
        self.assertEqual(self.run_(['ENTER'], 4), 'a1')                                # remembered
        self.assertEqual(self.run_(['/', 'DOWN', 'ENTER', 'ENTER'], 4), 'b1')         # back to titles
