"""Priority-driven picker order and the shared blank separator."""
import unittest

from recommended import SEPARATOR, SEPARATOR_PRIORITY, with_separator


class RecommendedTests(unittest.TestCase):
    def test_separator_sits_between_negative_priorities_and_subscription_rows(self):
        self.assertLess(SEPARATOR_PRIORITY, 1)

    def test_separator_follows_a_listed_recommendation_once_and_converges(self):
        catalog = {'other': True, 'models': [{'slug': 'chatgpt/sol', 'visibility': 'list', 'priority': 1},
                                             {'slug': 'fixture/a', 'visibility': 'list', 'priority': -3}]}
        once = with_separator(catalog)
        self.assertEqual([m['slug'] for m in once['models']], ['chatgpt/sol', 'fixture/a', SEPARATOR])
        self.assertEqual(with_separator(once), once)
        self.assertTrue(once['other'])
        self.assertEqual(len(catalog['models']), 2)

    def test_separator_is_removed_without_a_listed_recommendation(self):
        catalog = {'models': [{'slug': 'fixture/a', 'visibility': 'hide', 'priority': -3},
                              {'slug': 'fixture/b', 'visibility': 'list', 'priority': 90},
                              {'slug': SEPARATOR, 'visibility': 'list', 'priority': SEPARATOR_PRIORITY}]}
        self.assertEqual([m['slug'] for m in with_separator(catalog)['models']], ['fixture/a', 'fixture/b'])


if __name__ == '__main__':
    unittest.main()
