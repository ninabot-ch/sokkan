import unittest

from src.slug import slugify


class SlugTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_accents_are_folded(self):
        self.assertEqual(slugify("Été à Genève"), "ete-a-geneve")

    def test_runs_of_separators_become_one_dash(self):
        self.assertEqual(slugify("  Prix  :  50 %  !  "), "prix-50")

    def test_no_dash_at_either_end(self):
        self.assertEqual(slugify("--Bonjour--"), "bonjour")

    def test_cut_on_a_word_boundary(self):
        self.assertEqual(slugify("un titre beaucoup trop long pour une adresse", max_len=20),
                         "un-titre-beaucoup")

    def test_a_single_long_word_is_cut_hard(self):
        self.assertEqual(slugify("anticonstitutionnellement", max_len=10), "anticonsti")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")


if __name__ == "__main__":
    unittest.main()
