"""Offline prices, provenance, identity and canonical revision contracts."""
import copy
import unittest
from unittest.mock import patch

from catalog_enrichment import (HEADER, OPENAI_URL, MODELS_DEV_URL, enrich_catalog,
                                fetch_sources, parse_openai, validate_metadata, _fetch)

AT = '2026-10-05T00:00:00Z'
LATER = '2026-10-05T01:00:00Z'
STANDARD = {'tier': 'standard', 'context': 'short', 'input_per_million': 4,
            'output_per_million': 20, 'cached_input_per_million': 0.4, 'cache_write_per_million': 5}


def markdown():
    table = '| ' + ' | '.join(HEADER) + ' |\n|' + '|'.join(['---'] * 9) + '|\n'
    row = '| gpt-fixture | $4 | $0.4 | $5 | $20 | $8 | $0.8 | $10 | $30 |\n'
    return ('Flagship models\n' + ''.join(tier + '\n### ' + tier + ' pricing data\n' + table + row
            for tier in ('Standard', 'Flex', 'Fast')) +
            'Ultrafast\n### Ultrafast pricing data\n' + table + row.replace('$4', '$40') + 'Cyber models\n')


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'models': [{'slug': 'chatgpt/gpt-fixture', 'visibility': 'list'},
                                   {'slug': 'gpt-fixture', 'visibility': 'hide'},
                                   {'slug': 'claude/opus', 'visibility': 'list'},
                                   {'slug': 'modal/endpoint', 'visibility': 'list'}]}
        self.config = {'model_list': [
            {'model_name': 'chatgpt/gpt-fixture', 'litellm_params': {'model': 'chatgpt/gpt-fixture'}},
            {'model_name': 'claude/opus', 'litellm_params': {'model': 'claudesdk/claude-opus'}},
            {'model_name': 'modal/endpoint', 'litellm_params': {'model': 'openai/endpoint'}}]}
        self.manifest = {'models': [{'endpoint': 'endpoint', 'pricing_model': {'provider': 'zai', 'model': 'glm'}}]}
        self.sources = {'openai': {'gpt-fixture': [STANDARD]}, 'models_dev': {
            'anthropic': {'models': {'claude-opus': {'cost': {'input': 4, 'output': 20, 'cache_read': 0.2, 'cache_write': 5}}}},
            'zai': {'models': {'glm': {'cost': {'input': 0.15, 'output': 0.5}}}}}}

    def enrich(self, catalog=None, sources=None, at=AT):
        return enrich_catalog(catalog or self.catalog, self.config, self.manifest,
                              self.sources if sources is None else sources, at)

    def rows(self, result):
        return {r['model']: r for r in result['remote_codex']['models']}

    def test_published_prices_have_route_identity_and_separate_upstream_provenance(self):
        originals = copy.deepcopy((self.catalog, self.config, self.manifest, self.sources))
        result, summary = self.enrich()
        self.assertEqual(result['models'], self.catalog['models'])
        self.assertEqual((self.catalog, self.config, self.manifest, self.sources), originals)
        rows = self.rows(result)
        self.assertTrue(all(row['provider'] == 'litellm' for row in rows.values()))
        self.assertEqual(rows['chatgpt/gpt-fixture']['upstream'], {'provider': 'openai', 'model': 'gpt-fixture'})
        self.assertEqual(rows['chatgpt/gpt-fixture']['pricing']['rates'][0]['model'], 'chatgpt/gpt-fixture')
        self.assertEqual(rows['gpt-fixture']['pricing']['source_url'], OPENAI_URL)
        self.assertEqual(rows['claude/opus']['pricing']['source_url'], MODELS_DEV_URL)
        self.assertEqual(rows['modal/endpoint']['upstream'], {'provider': 'zai', 'model': 'glm'})
        self.assertEqual(summary['unpriced_models'], 0)

    def test_missing_price_and_hosted_endpoint_are_never_assumed_free(self):
        self.manifest = {'models': []}
        self.sources['openai'] = {'unselectable-model': [STANDARD]}
        result, summary = self.enrich()
        rows = self.rows(result)
        self.assertNotIn('pricing', rows['chatgpt/gpt-fixture'])
        self.assertNotIn('pricing', rows['modal/endpoint'])
        self.assertNotIn('upstream', rows['modal/endpoint'])
        self.assertNotIn('unselectable-model', rows)
        self.assertEqual(summary['unpriced_models'], 3)

    def test_public_free_listing_requires_explicit_zero_policy(self):
        self.sources['models_dev']['zai']['models']['glm']['cost'] = {'input': 0, 'output': 0}
        result, _ = self.enrich()
        self.assertNotIn('pricing', self.rows(result)['modal/endpoint'])

    def test_explicit_zero_policy_and_aliases_are_preserved_without_changing_stock_rows(self):
        route = self.config['model_list'][2]
        route['model_info'] = {'catalog_pricing': {'zero_rate': True, 'aliases': ['local-alias']}}
        result, _ = self.enrich()
        row = self.rows(result)['modal/endpoint']
        self.assertEqual(row['aliases'], ['local-alias'])
        self.assertEqual(row['pricing']['rates'][0]['input_per_million'], 0)
        self.assertEqual(row['pricing']['source_url'], 'local://configured-zero-rate')
        route['model_info']['catalog_pricing']['zero_rate'] = 'true'
        with self.assertRaises(ValueError):
            self.enrich()

    def test_failed_sources_and_invalid_individual_rates_retain_only_validated_stale_prices(self):
        old, _ = self.enrich()
        stale, summary = self.enrich(old, {'openai': None, 'models_dev': None}, LATER)
        self.assertEqual(summary['stale_prices'], 4)
        self.assertTrue(all(r['pricing']['status'] == 'stale' and r['pricing']['fetched_at'] == AT for r in self.rows(stale).values()))
        again, _ = self.enrich(stale, {'openai': None, 'models_dev': None}, LATER)
        self.assertEqual(again, stale)
        self.sources['models_dev']['anthropic']['models']['claude-opus']['cost']['input'] = -1
        changed, _ = self.enrich(old, at=LATER)
        self.assertEqual(self.rows(changed)['claude/opus']['pricing']['status'], 'stale')
        self.assertEqual(self.rows(changed)['modal/endpoint']['pricing']['status'], 'fresh')

    def test_successful_unknown_prices_clear_old_prices_and_new_identity_cannot_reuse_them(self):
        old, _ = self.enrich()
        changed, _ = self.enrich(old, {'openai': {}, 'models_dev': {}}, LATER)
        self.assertTrue(all('pricing' not in row for row in self.rows(changed).values()))
        self.config['model_list'][0]['litellm_params']['model'] = 'chatgpt/different'
        changed, _ = self.enrich(old, {'openai': None, 'models_dev': None})
        self.assertNotIn('pricing', self.rows(changed)['chatgpt/gpt-fixture'])

    def test_historical_prices_survive_inventory_removal_and_revision_tracks_capabilities(self):
        old, _ = self.enrich()
        removed = copy.deepcopy(old)
        removed['models'] = [r for r in removed['models'] if r['slug'] != 'claude/opus']
        result, _ = self.enrich(removed)
        self.assertEqual(result['models'], removed['models'])
        self.assertEqual(self.rows(result)['claude/opus']['pricing']['rates'][0]['input_per_million'], 4)
        changed = copy.deepcopy(old)
        changed['models'][0]['context_window'] = 400000
        changed, _ = self.enrich(changed)
        self.assertNotEqual(old['remote_codex']['revision'], changed['remote_codex']['revision'])

    def test_malformed_previous_metadata_is_not_a_cache_authority(self):
        old, _ = self.enrich()
        old['remote_codex']['models'][0]['pricing']['rates'][0]['output_per_million'] = -1
        changed, summary = self.enrich(old, {'openai': None, 'models_dev': None})
        self.assertEqual(summary['stale_prices'], 0)
        self.assertTrue(all('pricing' not in row for row in self.rows(changed).values()))
        with self.assertRaises(ValueError):
            validate_metadata({'schema_version': True, 'revision': 'a' * 64, 'models': []})

    def test_alias_ambiguity_rejects_complete_candidate(self):
        self.config['model_list'][0]['model_info'] = {'catalog_pricing': {'aliases': ['claude/opus']}}
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.enrich()

    def test_public_source_fetch_identifies_client_and_bounds_response(self):
        with patch('catalog_enrichment.urllib.request.urlopen') as open_url:
            response = open_url.return_value.__enter__.return_value
            response.read.return_value = b'{}'
            self.assertEqual(_fetch(MODELS_DEV_URL, 100), b'{}')
            request = open_url.call_args.args[0]
            self.assertEqual(request.full_url, MODELS_DEV_URL)
            self.assertEqual(request.get_header('User-agent'), 'litellm-deploy-model-refresh/1')
            self.assertEqual(request.get_header('Accept'), 'application/json')
            self.assertEqual(open_url.call_args.kwargs['timeout'], 10)
            response.read.assert_called_once_with(101)
            response.read.return_value = b'123'
            with self.assertRaisesRegex(ValueError, 'too large'):
                _fetch(MODELS_DEV_URL, 2)

    def test_official_tables_include_tiers_context_cache_rates_and_reject_incomplete_or_duplicate_data(self):
        result = parse_openai(markdown())
        self.assertEqual(len(result['gpt-fixture']), 6)
        self.assertEqual(result['gpt-fixture'][0]['cache_write_per_million'], 5)
        for bad in (markdown().replace('### Fast pricing data', '### Unknown pricing data'), markdown().replace('$4', '$-4'),
                    markdown().replace('Cyber models', markdown())):
            with self.assertRaises(ValueError):
                parse_openai(bad)
        with patch('catalog_enrichment._fetch', side_effect=OSError('injected source failure')):
            self.assertEqual(fetch_sources(), {'openai': None, 'models_dev': None})


if __name__ == '__main__':
    unittest.main()
