import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector import collect_source, extract_events, normalize_event, safe_url, unique_candidates

SOURCE = {"id": "test", "name": "Test", "url": "https://example.com/events/", "region": "asia", "topics": ["fintech"]}


class FakeFetcher:
    def __init__(self, robots):
        self.robots = robots
        self.requests = []

    def get(self, url, delay=None, robots=None):
        self.requests.append(url)
        if url.endswith("robots.txt"):
            return {"status": 200, "body": self.robots, "url": url}
        raise AssertionError("The denied event page must not be fetched")


class CollectorTests(unittest.TestCase):
    def test_robots_denial_prevents_page_fetch(self):
        fetcher = FakeFetcher("User-agent: *\nDisallow: /events/\n")
        status, candidates = collect_source(SOURCE, fetcher, "2026-11-04T00:00:00Z")
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(candidates, [])
        self.assertEqual(len(fetcher.requests), 1)

    def test_graph_itemlist_and_invalid_json(self):
        value = {"@graph": [{"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {"@type": ["https://schema.org/Event"], "name": "A", "startDate": "2027-01-05"}}]}]}
        document = '<script type="application/ld+json">' + json.dumps(value) + '</script><script type="application/ld+json">bad</script>'
        events, failures = extract_events(document)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "A")
        self.assertEqual(failures, 1)

    def test_duplicates_keep_review_state(self):
        raw = {"@type": "Event", "name": "A", "startDate": "2027-01-05", "endDate": "2027-01-06"}
        first = normalize_event(raw, SOURCE, SOURCE["url"], "first")
        second = normalize_event(raw, SOURCE, SOURCE["url"], "second")
        first["reviewStatus"] = "approved"
        result = unique_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["reviewStatus"], "approved")

    def test_missing_or_invalid_dates_are_not_invented(self):
        record = normalize_event({"name": "A", "startDate": "2027-02-30"}, SOURCE, SOURCE["url"], "now")
        self.assertEqual(record["startDate"], "")
        self.assertEqual(record["endDate"], "")
        self.assertIn("startDate", record["missingFields"])
        self.assertEqual(record["verification"], "pending")

    def test_local_and_non_https_urls_rejected(self):
        for url in ("http://example.com", "https://127.0.0.1", "https://192.168.1.10", "https://[::1]", "https://localhost", "https://user:pass@example.com", "https://example.com:8443"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    safe_url(url)


if __name__ == "__main__":
    unittest.main()
