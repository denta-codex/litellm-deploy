"""Recommended picker order and the shared blank separator."""
import unittest

from recommended import RECOMMENDED, SEPARATOR, SEPARATOR_PRIORITY, priority, with_separator


class RecommendedTests(unittest.TestCase):
    def test_recommended_rows_sort_in_order_above_separator_and_subscription_rows(self):
        ranks = [priority(slug, 80) for slug in RECOMMENDED]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(max(ranks), SEPARATOR_PRIORITY)
        self.assertLess(SEPARATOR_PRIORITY, 1)
        self.assertEqual(priority('bedrock/opus-5.5', 85), 85)

    def test_separator_follows_a_listed_recommendation_once_and_converges(self):
        catalog = {'other': True, 'models': [{'slug': 'chatgpt/sol', 'visibility': 'list'},
                                             {'slug': 'claude/opus-5.5', 'visibility': 'list'}]}
        once = with_separator(catalog)
        self.assertEqual([m['slug'] for m in once['models']], ['chatgpt/sol', 'claude/opus-5.5', SEPARATOR])
        self.assertEqual(with_separator(once), once)
        self.assertTrue(once['other'])
        self.assertEqual(len(catalog['models']), 2)

    def test_separator_is_removed_without_a_listed_recommendation(self):
        catalog = {'models': [{'slug': 'claude/opus-5.5', 'visibility': 'hide'},
                              {'slug': SEPARATOR, 'visibility': 'list'}]}
        self.assertEqual([m['slug'] for m in with_separator(catalog)['models']], ['claude/opus-5.5'])


if __name__ == '__main__':
    unittest.main()
