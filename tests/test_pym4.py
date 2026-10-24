import tempfile
import unittest
from pathlib import Path

from pym4 import M4Error, M4Lite, main


class M4LiteTests(unittest.TestCase):
    def test_defines_and_expands_positional_parameters(self):
        result = M4Lite().expand("define(`pair', `($1,$2)')pair(3,4)")
        self.assertEqual(result, "(3,4)")

    def test_balanced_quotes_protect_macro_text(self):
        result = M4Lite().expand("define(`name', `expanded')`name'")
        self.assertEqual(result, "name")

    def test_diversion_discards_until_divert_zero(self):
        result = M4Lite().expand("divert(-1)hidden\ndivert(0)visible")
        self.assertEqual(result, "visible")

    def test_nested_arithmetic_and_string_indexing(self):
        result = M4Lite().expand("eval(2+3*4):index(`abcd',`c')")
        self.assertEqual(result, "14:2")

    def test_patsubst_replaces_a_regex_match(self):
        result = M4Lite().expand("patsubst(`part42',`[0-9]+',`X')")
        self.assertEqual(result, "partX")

    def test_shift_drops_the_first_argument(self):
        result = M4Lite().expand("shift(`a',`b',`c')")
        self.assertEqual(result, "b,c")

    def test_include_resolves_relative_to_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "macros.m4").write_text("define(`word', `hello')", encoding="utf-8")
            input_file = root / "input.m4"
            result = M4Lite().expand("include(`macros.m4')word", input_file)
            self.assertEqual(result, "hello")

    def test_include_falls_back_to_windows_1252(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy.m4").write_bytes(b"caf\xe9")
            input_file = root / "input.m4"
            result = M4Lite().expand("include(`legacy.m4')", input_file)
            self.assertEqual(result, "café")

    def test_rejects_unterminated_macro_call(self):
        with self.assertRaisesRegex(M4Error, "argument list"):
            M4Lite().expand("define(`name', `value'")

    def test_dollar_at_requotes_arguments(self):
        result = M4Lite().expand("define(`x',`X')define(`q',`[$@]')q(`x',y)")
        self.assertEqual(result, "[x,y]")

    def test_recursive_loop_macro_like_circuit_macros_for_(self):
        source = ("define(`for_',`ifelse(eval(`$1'!=(`$2'+(`$3'))),1,"
                  "`pushdef(`m4x',`$1')$4`'popdef(`m4x')for_(eval(`$1'+(`$3')),`$2',`$3',`$4')')')"
                  "for_(1,3,1,`m4x ')")
        self.assertEqual(M4Lite().expand(source), "1 2 3 ")

    def test_regexp_groups_and_translit_ranges(self):
        result = M4Lite().expand("regexp(`abc123',`\\([a-z]*\\)\\([0-9]*\\)',`\\2-\\1') translit(`hello',`a-z',`A-Z')")
        self.assertEqual(result, "123-abc HELLO")

    def test_defn_changequote_and_c_style_eval(self):
        m4 = M4Lite()
        result = m4.expand("define(`a',`A')define(`b',defn(`a'))b "
                           "eval(-7/2) eval(7%-3) eval(2**10) eval(1<<31)"
                           "changequote([,])define([x],[y]) x")
        self.assertEqual(result, "A -3 1 1024 -2147483648 y")

    def test_regexp_alternation_is_leftmost_longest_like_gnu(self):
        result = M4Lite().expand("regexp(`LMBS',`L\\(M\\|B\\|MB\\|BM\\)?',`\\&')")
        self.assertEqual(result, "LMB")

    def test_macro_name_can_take_arguments_after_its_expansion(self):
        result = M4Lite().expand("define(`f',`<$1>')ifelse(a,a,`f')(1)")
        self.assertEqual(result, "<1>")


class CommandLineTests(unittest.TestCase):
    def test_expands_a_file_using_an_include_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib").mkdir()
            (root / "lib" / "macros.m4").write_text("define(`word', `hello')", encoding="utf-8")
            (root / "input.m4").write_text("include(`macros.m4')word\n", encoding="utf-8")
            output = root / "out.txt"
            code = main(["-I", str(root / "lib"), "-o", str(output), str(root / "input.m4")])
            self.assertEqual((code, output.read_text(encoding="utf-8")), (0, "hello\n"))


if __name__ == "__main__":
    unittest.main()