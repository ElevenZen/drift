"""Unit tests for format-preserving TOML text editing (src/drift/utils/toml_edit.py)."""

import unittest

from drift.utils.toml_edit import (
    TomlEditAction,
    TomlLineKind,
    find_toml_table_span,
    format_toml_key,
    scan_toml_logical_lines,
    set_toml_table_bools,
    split_toml_dotted_key,
)
from drift.utils.toml_utils import parse_toml

TABLE = ("packages", "enable")


def _set(text, updates, create=False):
    return set_toml_table_bools(text, TABLE, updates, create_table_if_missing=create)


class TestTomlKeyHelpers(unittest.TestCase):
    def test_format_toml_key_bare_and_quoted(self) -> None:
        self.assertEqual(format_toml_key("tmux"), "tmux")
        self.assertEqual(format_toml_key("my-pkg_2"), "my-pkg_2")
        self.assertEqual(format_toml_key("my.pkg"), '"my.pkg"')
        self.assertEqual(format_toml_key("with space"), '"with space"')
        self.assertEqual(format_toml_key('q"uote'), '"q\\"uote"')
        with self.assertRaises(ValueError):
            format_toml_key("")

    def test_split_toml_dotted_key(self) -> None:
        self.assertEqual(split_toml_dotted_key("packages.enable"), ["packages", "enable"])
        self.assertEqual(split_toml_dotted_key(' packages . "en.able" '), ["packages", "en.able"])
        self.assertEqual(split_toml_dotted_key("'a.b'"), ["a.b"])
        self.assertEqual(split_toml_dotted_key("plain"), ["plain"])


class TestScanLogicalLines(unittest.TestCase):
    def test_classifies_headers_assignments_and_skips_comments(self) -> None:
        text = (
            "# [packages.enable]\n"
            "top = 1\n"
            "[packages.enable]\n"
            "a = true # c\n"
            "[[arr]]\n"
            "x = [\n  1,\n  2,\n]\n"
        )
        lines = scan_toml_logical_lines(text)
        kinds = [ln.kind for ln in lines]
        self.assertEqual(
            kinds,
            [TomlLineKind.ASSIGNMENT, TomlLineKind.HEADER, TomlLineKind.ASSIGNMENT,
             TomlLineKind.ARRAY_HEADER, TomlLineKind.ASSIGNMENT],
        )
        self.assertEqual(lines[1].table_keys, TABLE)
        self.assertEqual((lines[4].start, lines[4].end), (5, 8))
        self.assertTrue(lines[4].is_multiline)

    def test_header_like_text_inside_multiline_string_is_ignored(self) -> None:
        text = 's = """\n[packages.enable]\n"""\n'
        self.assertIsNone(find_toml_table_span(scan_toml_logical_lines(text), TABLE))

    def test_unclosed_delimiters_raise(self) -> None:
        with self.assertRaises(ValueError):
            scan_toml_logical_lines("a = [1, 2\n")

    def test_span_stops_at_next_header(self) -> None:
        text = "[packages.enable]\na = true\n[render.x]\nb = 1\n"
        span = find_toml_table_span(scan_toml_logical_lines(text), TABLE)
        self.assertIsNotNone(span)
        assert span is not None
        self.assertEqual([a.key_path for a in span.assignments], [("a",)])
        self.assertEqual(span.insert_after, 1)


class TestSetTomlTableBools(unittest.TestCase):
    def test_update_preserves_comment_indent_and_spacing(self) -> None:
        text = "[packages.enable]\n  tmux   =   false   # only dev box\n"
        res = _set(text, {"tmux": True})
        self.assertEqual(res.text, "[packages.enable]\n  tmux   =   true   # only dev box\n")
        self.assertEqual(res.actions, {"tmux": TomlEditAction.UPDATED})
        self.assertTrue(res.changed)

    def test_unchanged_returns_identical_text(self) -> None:
        text = "[packages.enable]\ntmux = true # keep\n"
        res = _set(text, {"tmux": True})
        self.assertEqual(res.text, text)
        self.assertEqual(res.actions, {"tmux": TomlEditAction.UNCHANGED})
        self.assertFalse(res.changed)

    def test_non_bool_existing_value_is_normalized(self) -> None:
        res = _set('[packages.enable]\ntmux = "yes"\n', {"tmux": True})
        self.assertEqual(res.text, "[packages.enable]\ntmux = true\n")
        self.assertEqual(res.actions["tmux"], TomlEditAction.UPDATED)

    def test_insert_after_last_assignment_before_banner(self) -> None:
        text = (
            "[packages.enable]\n"
            "DEFAULT = false\n"
            "\n"
            "# ----- Render Engines -----\n"
            "[render.envsubst]\n"
            'suffix = "envst"\n'
        )
        res = _set(text, {"tmux": True})
        self.assertEqual(
            res.text,
            "[packages.enable]\nDEFAULT = false\ntmux = true\n\n# ----- Render Engines -----\n"
            '[render.envsubst]\nsuffix = "envst"\n',
        )

    def test_insert_into_empty_section_and_copy_indent(self) -> None:
        self.assertEqual(
            _set("[packages.enable]\n\n[w]\n", {"a": True}).text,
            "[packages.enable]\na = true\n\n[w]\n",
        )
        self.assertEqual(
            _set("[packages.enable]\n    a = true\n", {"b": False}).text,
            "[packages.enable]\n    a = true\n    b = false\n",
        )

    def test_insert_after_multiline_value(self) -> None:
        text = '[packages.enable]\nnote = """\nx\n"""\n'
        res = _set(text, {"a": True})
        self.assertEqual(res.text, '[packages.enable]\nnote = """\nx\n"""\na = true\n')

    def test_quoted_existing_key_is_matched_and_new_key_is_quoted(self) -> None:
        res = _set('[packages.enable]\n"my.pkg" = false\n', {"my.pkg": True, "o.k": True})
        self.assertEqual(res.text, '[packages.enable]\n"my.pkg" = true\n"o.k" = true\n')
        self.assertEqual(parse_toml(res.text)["packages"]["enable"], {"my.pkg": True, "o.k": True})

    def test_commented_header_is_not_a_table(self) -> None:
        with self.assertRaises(ValueError):
            _set("# [packages.enable]\n# a = true\n", {"a": True})

    def test_crlf_and_missing_trailing_newline_preserved(self) -> None:
        text = "[packages.enable]\r\na = false\r\n[w]\r\nx = 1"
        res = _set(text, {"a": True, "b": True})
        self.assertEqual(res.text, "[packages.enable]\r\na = true\r\nb = true\r\n[w]\r\nx = 1")

        tail = _set("[packages.enable]\na = true", {"b": False})
        self.assertEqual(tail.text, "[packages.enable]\na = true\nb = false")

    def test_mixed_multiple_updates(self) -> None:
        text = "[packages.enable]\na = true\nb = false\n"
        res = _set(text, {"a": True, "b": True, "c": False})
        self.assertEqual(res.text, "[packages.enable]\na = true\nb = true\nc = false\n")
        self.assertEqual(
            res.actions,
            {"a": TomlEditAction.UNCHANGED, "b": TomlEditAction.UPDATED, "c": TomlEditAction.INSERTED},
        )

    def test_missing_table_raises_or_is_appended(self) -> None:
        with self.assertRaises(ValueError):
            _set("[w]\nx = 1\n", {"a": True})
        self.assertEqual(_set("", {"a": True}, create=True).text, "[packages.enable]\na = true\n")
        self.assertEqual(
            _set("[w]\nx = 1", {"a": True}, create=True).text,
            "[w]\nx = 1\n\n[packages.enable]\na = true\n",
        )
        self.assertEqual(
            _set("[w]\r\nx = 1\r\n", {"a": True}, create=True).text,
            "[w]\r\nx = 1\r\n\r\n[packages.enable]\r\na = true\r\n",
        )

    def test_unsupported_shapes_raise(self) -> None:
        with self.assertRaises(ValueError):
            _set("[packages.enable]\na = [\n true,\n]\n", {"a": True})
        with self.assertRaises(ValueError):
            _set("[packages.enable]\na.b = true\n", {"a": True})
        with self.assertRaises(ValueError):
            _set("[packages.enable]\n", {"a": "yes"})  # type: ignore[dict-item]
        with self.assertRaises(ValueError):
            set_toml_table_bools("", (), {"a": True})

    def test_two_pass_idempotency(self) -> None:
        text = "[packages.enable]\nDEFAULT = false\n"
        first = _set(text, {"a": True, "b": False})
        second = _set(first.text, {"a": True, "b": False})
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.text, first.text)

    def test_unrelated_content_byte_identical(self) -> None:
        text = (
            "# header comment\n"
            "[env.default]\n"
            'HOME_DIR = "${HOME}/x"  # keep\n'
            "\n"
            "[packages.enable]\n"
            "DEFAULT = false\n"
            "\n"
            "[render.j2]\n"
            'render_command = "jinja2 %s %i"\n'
        )
        res = _set(text, {"tmux": True})
        self.assertEqual(res.text.replace("tmux = true\n", "", 1), text)


if __name__ == "__main__":
    unittest.main()
