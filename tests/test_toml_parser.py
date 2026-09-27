import unittest

from drift.utils.toml_utils import (
    parse_toml,
    _parse_toml_fallback,
    parse_toml_value,
    split_array_elements,
    dump_toml,
)


class TestParseTomlValue(unittest.TestCase):
    """Targeted tests for parse_toml_value and its scalar/array tokenizer helpers."""

    def test_parse_toml_value_strings(self) -> None:
        self.assertEqual(parse_toml_value('"hello"'), "hello")
        self.assertEqual(parse_toml_value("'world'"), "world")
        self.assertEqual(parse_toml_value('"escaped \\" quote"'), 'escaped " quote')
        self.assertEqual(parse_toml_value('"new\\nline"'), "new\nline")

    def test_parse_toml_value_booleans(self) -> None:
        self.assertEqual(parse_toml_value('true'), True)
        self.assertEqual(parse_toml_value('FALSE'), False)

    def test_parse_toml_value_numbers(self) -> None:
        self.assertEqual(parse_toml_value('42'), 42)
        self.assertEqual(parse_toml_value('-12'), -12)
        self.assertEqual(parse_toml_value('3.14'), 3.14)
        self.assertEqual(parse_toml_value('-0.5'), -0.5)

    def test_parse_toml_value_fallback_unquoted(self) -> None:
        self.assertEqual(parse_toml_value('unquoted_str'), 'unquoted_str')
        self.assertEqual(parse_toml_value(''), "")

    def test_split_array_elements_basic(self) -> None:
        self.assertEqual(split_array_elements('"a", "b", "c"'), ['"a"', '"b"', '"c"'])
        self.assertEqual(split_array_elements('"a, b", "c"'), ['"a, b"', '"c"'])
        self.assertEqual(split_array_elements("'a', 'b'"), ["'a'", "'b'"])

    def test_split_array_elements_nested_structures(self) -> None:
        # Nested brackets
        self.assertEqual(
            split_array_elements('[1, 2], [3, 4]'),
            ['[1, 2]', '[3, 4]']
        )
        # Nested braces (inline tables)
        self.assertEqual(
            split_array_elements('{ a = 1, b = 2 }, { c = 3 }'),
            ['{ a = 1, b = 2 }', '{ c = 3 }']
        )
        # Mixed nested brackets and braces
        self.assertEqual(
            split_array_elements('[{ name = "git", optional = true }], "base"'),
            ['[{ name = "git", optional = true }]', '"base"']
        )

    def test_split_array_elements_trailing_comma(self) -> None:
        self.assertEqual(split_array_elements('1, 2, 3,'), ['1', '2', '3'])
        self.assertEqual(split_array_elements('"a", "b",'), ['"a"', '"b"'])
        # Empty string literal element should be preserved, trailing whitespace ignored
        self.assertEqual(split_array_elements('"", "non-empty", '), ['""', '"non-empty"'])

    def test_split_array_elements_unclosed_or_unmatched_raises(self) -> None:
        # Unclosed brackets, braces, or quotes
        with self.assertRaises(ValueError):
            split_array_elements('1, 2, [3, 4')
        with self.assertRaises(ValueError):
            split_array_elements('{ a = 1')
        with self.assertRaises(ValueError):
            split_array_elements('"unclosed')
        with self.assertRaises(ValueError):
            split_array_elements("'unclosed")
        # Unmatched closing delimiters
        with self.assertRaises(ValueError):
            split_array_elements('1, 2], 3')
        with self.assertRaises(ValueError):
            split_array_elements('1, 2}, 3')

    def test_parse_toml_array_value_primitives(self) -> None:
        # String arrays
        self.assertEqual(parse_toml_value('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_toml_value('[]'), [])
        self.assertEqual(parse_toml_value('[   ]'), [])
        # Integer and float arrays
        self.assertEqual(parse_toml_value('[1, 2, 3]'), [1, 2, 3])
        self.assertEqual(parse_toml_value('[1.5, -2.5, 3.0]'), [1.5, -2.5, 3.0])
        # Boolean arrays
        self.assertEqual(parse_toml_value('[true, false, true]'), [True, False, True])
        # Mixed primitives
        self.assertEqual(parse_toml_value('[1, "hello", true, 3.14]'), [1, "hello", True, 3.14])

    def test_parse_toml_array_value_commas_and_escapes(self) -> None:
        # Commas within quotes should not be treated as delimiters
        self.assertEqual(parse_toml_value('["a, b", "c, d, e"]'), ["a, b", "c, d, e"])
        self.assertEqual(parse_toml_value("['single, comma', 'second']"), ["single, comma", "second"])
        # Escapes
        self.assertEqual(parse_toml_value('["line1\\nline2", "tab\\tval"]'), ["line1\nline2", "tab\tval"])

    def test_parse_toml_array_value_nested_arrays(self) -> None:
        # 2D arrays
        self.assertEqual(parse_toml_value('[[1, 2], [3, 4]]'), [[1, 2], [3, 4]])
        self.assertEqual(parse_toml_value('[[], ["a", "b"]]'), [[], ["a", "b"]])
        # 3D arrays
        self.assertEqual(parse_toml_value('[[[1]], [[2]]]'), [[[1]], [[2]]])

    def test_parse_toml_array_value_inline_tables(self) -> None:
        # Inline table alone
        self.assertEqual(parse_toml_value('{ name = "git", optional = true }'), {"name": "git", "optional": True})
        self.assertEqual(parse_toml_value('{}'), {})
        # Array of inline tables
        self.assertEqual(
            parse_toml_value('[{ name = "git", optional = true }, { name = "base", optional = false }]'),
            [{"name": "git", "optional": True}, {"name": "base", "optional": False}]
        )
        # Array with mixed strings and inline tables
        self.assertEqual(
            parse_toml_value('["base", { name = "git", optional = true }]'),
            ["base", {"name": "git", "optional": True}]
        )

    def test_parse_toml_nested_inline_tables(self) -> None:
        # Nested inline tables are supported by TOML spec and parsed recursively
        self.assertEqual(
            parse_toml_value('{ outer = { inner = "val", count = 42 }, active = true }'),
            {"outer": {"inner": "val", "count": 42}, "active": True}
        )
        self.assertEqual(
            parse_toml_value('{ a = { b = { c = "deep" } } }'),
            {"a": {"b": {"c": "deep"}}}
        )
        # Array containing nested inline tables
        self.assertEqual(
            parse_toml_value('[{ dep = { name = "nvim", config = { lazy = true } } }]'),
            [{"dep": {"name": "nvim", "config": {"lazy": True}}}]
        )

    def test_parse_toml_array_the_void(self) -> None:
        # Deeply nested empty arrays (from toml-test empty.toml)
        self.assertEqual(parse_toml_value("[[[[[]]]]]"), [[[[[]]]]])
        self.assertEqual(parse_toml_value("[[], []]"), [[], []])

    def test_parse_toml_array_nospaces(self) -> None:
        # Compact arrays with no spaces (from toml-test array-nospaces.toml)
        self.assertEqual(parse_toml_value("[1,2,3]"), [1, 2, 3])
        self.assertEqual(parse_toml_value("[[1,2],[3,4]]"), [[1, 2], [3, 4]])

    def test_parse_toml_array_heterogeneous(self) -> None:
        # Arrays with mixed scalar types (from toml-test heterogeneous.toml)
        self.assertEqual(parse_toml_value('[1, "two", 3.14, true, false]'), [1, "two", 3.14, True, False])


class TestFallbackTomlParser(unittest.TestCase):
    """Dedicated tests for _parse_toml_fallback and standard TOML parsing."""

    def setUp(self) -> None:
        self.parsers = [parse_toml, _parse_toml_fallback]

    def test_parse_toml_basic(self) -> None:
        toml_str = """
        # Global Comment
        key = "value"  # Inline comment
        number = 42
        enabled = true
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data.get("key"), "value")
            self.assertEqual(data.get("number"), 42)
            self.assertEqual(data.get("enabled"), True)

    def test_parse_toml_with_tables(self) -> None:
        toml_str = """
        [workspace]
        render_directory = "my_render"
        install_directory = "my_install"

        [packages.enable]
        shell = true
        nvim = false
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("workspace", data)
            self.assertEqual(data["workspace"]["render_directory"], "my_render")
            self.assertEqual(data["workspace"]["install_directory"], "my_install")
            self.assertIn("packages", data)
            self.assertEqual(data["packages"]["enable"]["shell"], True)
            self.assertEqual(data["packages"]["enable"]["nvim"], False)

    def test_parse_toml_multiline_array(self) -> None:
        toml_str = """
        [package]
        name = "test_pkg"
        fully_controlled_dirs = [
            "dir1", # Comment inside
            "dir2"  # Another comment
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("package", data)
            self.assertEqual(data["package"]["name"], "test_pkg")
            self.assertEqual(data["package"]["fully_controlled_dirs"], ["dir1", "dir2"])

    def test_parse_toml_multiline_array_with_trailing_comma(self) -> None:
        toml_str = """
        [package]
        items = [
            "item1",
            "item2",
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["package"]["items"], ["item1", "item2"])

    def test_parse_toml_multiline_nested_arrays(self) -> None:
        toml_str = """
        [matrix]
        grid = [
            [1, 2],
            [3, 4],
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["matrix"]["grid"], [[1, 2], [3, 4]])

    def test_parse_toml_dotted_tables(self) -> None:
        toml_str = """
        [table.subtable]
        val = "nested"
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("table", data)
            self.assertIn("subtable", data["table"])
            self.assertEqual(data["table"]["subtable"]["val"], "nested")

    def test_parse_toml_array_of_tables(self) -> None:
        toml_str = """
        [package]
        name = "neovim"

        [[package.dependencies]]
        name = "base"
        optional = false

        [[package.dependencies]]
        name = "git"
        optional = true
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("package", data)
            self.assertEqual(data["package"]["name"], "neovim")
            deps = data["package"]["dependencies"]
            self.assertEqual(len(deps), 2)
            self.assertEqual(deps[0], {"name": "base", "optional": False})
            self.assertEqual(deps[1], {"name": "git", "optional": True})

    def test_parse_toml_array_subtables(self) -> None:
        # Array of tables with subtables (from toml-test array-subtables.toml)
        toml_str = """
        [[arr]]
        [arr.subtab]
        val = 1

        [[arr]]
        [arr.subtab]
        val = 2
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("arr", data)
            self.assertEqual(len(data["arr"]), 2)
            self.assertEqual(data["arr"][0]["subtab"]["val"], 1)
            self.assertEqual(data["arr"][1]["subtab"]["val"], 2)

    def test_parse_toml_array_the_void_doc(self) -> None:
        # Deeply nested empty array in document
        toml_str = 'thevoid = [[[[[]]]]]'
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["thevoid"], [[[[[]]]]])

    def test_parse_toml_array_nospaces_doc(self) -> None:
        # Document with compact arrays without spaces
        toml_str = """
        ints=[1,2,3]
        nested=[[1,2],[3,4]]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["ints"], [1, 2, 3])
            self.assertEqual(data["nested"], [[1, 2], [3, 4]])

    def test_parse_toml_array_heterogeneous_doc(self) -> None:
        # Document with heterogeneous arrays
        toml_str = """
        mixed = [1, "two", 3.14, true, false]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["mixed"], [1, "two", 3.14, True, False])

    def test_parse_toml_array_escaped_quotes_and_comments(self) -> None:
        # Array with escaped quotes, backslashes, and hash marks inside string literals
        toml_str = r"""
        items = [
            "item with \" # hash",
            "item with \\ backslash",
            "normal item" # trailing comment
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["items"], ['item with " # hash', 'item with \\ backslash', 'normal item'])

    def test_parse_toml_array_interspersed_comments_and_blank_lines(self) -> None:
        # Array with comments on separate lines, trailing comma, and empty lines
        toml_str = """
        [package]
        dependencies = [
            # Base requirements
            "core-utils",

            # Terminal tools
            "zsh", # shell
            "tmux",

            # Editor
            { name = "neovim", optional = true },
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(
                data["package"]["dependencies"],
                [
                    "core-utils",
                    "zsh",
                    "tmux",
                    {"name": "neovim", "optional": True},
                ]
            )

    def test_parse_toml_array_of_inline_tables_with_arrays(self) -> None:
        # Array containing inline tables that themselves contain nested arrays
        toml_str = """
        rules = [
            { id = 1, tags = ["fast", "safe"] },
            { id = 2, tags = [] },
            { id = 3, tags = ["slow"] },
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(
                data["rules"],
                [
                    {"id": 1, "tags": ["fast", "safe"]},
                    {"id": 2, "tags": []},
                    {"id": 3, "tags": ["slow"]},
                ]
            )

    def test_parse_toml_nested_mixed_arrays(self) -> None:
        # 2D nested arrays with mixed types
        toml_str = """
        matrix = [
            [1, 2, 3],
            ["a", "b", "c"],
            [true, false],
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(
                data["matrix"],
                [
                    [1, 2, 3],
                    ["a", "b", "c"],
                    [True, False],
                ]
            )

    def test_parse_toml_nested_inline_tables_doc(self) -> None:
        toml_str = """
        [package]
        name = "editor"
        metadata = { author = { name = "Alice", team = "Dev" }, version = 1 }
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data["package"]["name"], "editor")
            self.assertEqual(
                data["package"]["metadata"],
                {"author": {"name": "Alice", "team": "Dev"}, "version": 1}
            )

    def test_parse_toml_unclosed_quotes_or_brackets(self) -> None:
        bad_toml = 'key = "unclosed string'
        with self.assertRaises(ValueError):
            _parse_toml_fallback(bad_toml)

        bad_array = 'key = [1, 2, 3'
        with self.assertRaises(ValueError):
            _parse_toml_fallback(bad_array)


class TestDumpToml(unittest.TestCase):
    """Unit tests for dump_toml serialization, multiline escaping, and roundtrip parsing."""

    def test_dump_toml_multiline_string_escaping(self) -> None:
        data = {
            "package": {
                "name": "my_pkg",
                "description": "Line 1\nLine 2\nLine 3",
                "notes": "Tab:\t, Windows Line:\r\n, Backslash: \\, Quotes: \"Hello\"",
            },
            "env": {
                "override": {
                    "SCRIPT": "echo 'hello'\necho 'world'\n",
                }
            }
        }

        toml_str = dump_toml(data)
        # Ensure raw literal line breaks are NOT in the serialized string literals
        for line in toml_str.splitlines():
            if line.startswith("description ="):
                self.assertIn(r"\n", line)
                self.assertNotIn("\n", line[len("description ="):])

        parsed = parse_toml(toml_str)
        self.assertEqual(parsed["package"]["name"], "my_pkg")
        self.assertEqual(parsed["package"]["description"], "Line 1\nLine 2\nLine 3")
        self.assertEqual(parsed["package"]["notes"], "Tab:\t, Windows Line:\r\n, Backslash: \\, Quotes: \"Hello\"")
        self.assertEqual(parsed["env"]["override"]["SCRIPT"], "echo 'hello'\necho 'world'\n")

    def test_dump_toml_roundtrip_data_types(self) -> None:
        data = {
            "version": 1,
            "debug": True,
            "ratio": 3.14,
            "tags": ["a\nb", "c", "d"],
            "empty": None,
            "package": {
                "enable_render": False,
                "count": 42,
            }
        }

        toml_str = dump_toml(data)
        parsed = parse_toml(toml_str)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["debug"], True)
        self.assertEqual(parsed["ratio"], 3.14)
        self.assertEqual(parsed["tags"], ["a\nb", "c", "d"])
        self.assertNotIn("empty", parsed)
        self.assertEqual(parsed["package"]["enable_render"], False)
        self.assertEqual(parsed["package"]["count"], 42)

    def test_dump_toml_array_types(self) -> None:
        data = {
            "config": {
                "empty_list": [],
                "string_list": ["apple", "banana", "cherry"],
                "int_list": [10, 20, 30],
                "bool_list": [True, False, True],
            }
        }
        toml_str = dump_toml(data)
        parsed = parse_toml(toml_str)
        self.assertEqual(parsed["config"]["empty_list"], [])
        self.assertEqual(parsed["config"]["string_list"], ["apple", "banana", "cherry"])
        self.assertEqual(parsed["config"]["int_list"], [10, 20, 30])
        self.assertEqual(parsed["config"]["bool_list"], [True, False, True])


if __name__ == "__main__":
    unittest.main()
