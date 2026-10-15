import unittest

from d1ffler.three_way_merge import merge_text


class ThreeWayMergeTests(unittest.TestCase):
    def test_independent_edits_merge(self):
        result = merge_text("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n")
        self.assertEqual(result.text, "A\nb\nC\n")
        self.assertEqual(result.automatic_merges, 2)
        self.assertEqual(result.conflicts, ())

    def test_adjacent_edits_do_not_conflict(self):
        result = merge_text("a\nb\n", "A\nb\n", "a\nB\n")
        self.assertEqual(result.text, "A\nB\n")
        self.assertEqual(result.conflicts, ())

    def test_identical_edits_apply_once(self):
        result = merge_text("a\n", "A\n", "A\n")
        self.assertEqual(result.text, "A\n")
        self.assertEqual(result.automatic_merges, 1)

    def test_competing_replacements_report_base_range(self):
        result = merge_text("a\nb\nc\n", "a\nL\nc\n", "a\nI\nc\n")
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual((conflict.start, conflict.end), (1, 2))
        self.assertEqual((conflict.base, conflict.local, conflict.incoming),
                         ("b\n", "L\n", "I\n"))
        self.assertIn(conflict.marker, result.text)

    def test_competing_insertions_conflict(self):
        result = merge_text("a\nb\n", "a\nL\nb\n", "a\nI\nb\n")
        self.assertEqual((result.conflicts[0].start, result.conflicts[0].end), (1, 1))

    def test_insertion_at_replacement_boundary_is_independent(self):
        result = merge_text("a\nb\n", "x\na\nb\n", "A\nb\n")
        self.assertEqual(result.text, "x\nA\nb\n")
        self.assertEqual(result.conflicts, ())

    def test_no_final_newline_survives_merge(self):
        result = merge_text("a\nb", "A\nb", "a\nB")
        self.assertEqual(result.text, "A\nB")
        self.assertEqual(result.conflicts, ())


if __name__ == "__main__":
    unittest.main()
