import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector import (
    collect_source,
    extract_events,
    normalize_event,
    safe_url,
    unique_candidates,
    page_records,
    rotate_links,
)

SOURCE = {
    "id": "test",
    "name": "Test",
    "url": "https://example.com/events/",
    "region": "asia",
    "topics": ["fintech"],
}


class QubeScheduleTests(unittest.TestCase):
    def test_reads_detail_header_not_the_series_calendar(self):
        from collector import qube_detail_schedule
        url = "https://www.qubevents.com/africa-payments"
        source = dict(url=url,nameEn="Africa Payments & RegTech Forum 2027",
                      programmeLocation=dict(city="Johannesburg",country="South Africa",
                          printedCountry="South Africa",heading="Africa Payments & RegTech Forum"))
        text = "Africa Payments & RegTech Forum 17 JUNE 2027 Marriott Hotel, Johannesburg, South Africa REGISTER HERE"
        self.assertEqual(qube_detail_schedule(url,source,text)[0]["startDate"],"2027-06-17")
        self.assertEqual(qube_detail_schedule(url + "-2026",source,text),[])
        self.assertEqual(qube_detail_schedule(url,source,text.replace("Johannesburg","Cape Town")),[])
        self.assertEqual(qube_detail_schedule(url,source,text.replace("17 JUNE","31 JUNE")),[])


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
        value = {
            "@graph": [
                {
                    "@type": "ItemList",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "item": {
                                "@type": ["https://schema.org/Event"],
                                "name": "A",
                                "startDate": "2027-01-05",
                            },
                        }
                    ],
                }
            ]
        }
        document = (
            '<script type="application/ld+json">'
            + json.dumps(value)
            + '</script><script type="application/ld+json">bad</script>'
        )
        events, failures = extract_events(document)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "A")
        self.assertEqual(failures, 1)

    def test_duplicates_keep_review_state(self):
        raw = {
            "@type": "Event",
            "name": "A",
            "startDate": "2027-01-05",
            "endDate": "2027-01-06",
        }
        first = normalize_event(raw, SOURCE, SOURCE["url"], "first")
        second = normalize_event(raw, SOURCE, SOURCE["url"], "second")
        first["reviewStatus"] = "approved"
        result = unique_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["reviewStatus"], "approved")
        self.assertEqual(result[0]["collectedAt"], "second")

    def test_missing_or_invalid_dates_are_not_invented(self):
        record = normalize_event(
            {"name": "A", "startDate": "2027-02-30"}, SOURCE, SOURCE["url"], "now"
        )
        self.assertEqual(record["startDate"], "")
        self.assertEqual(record["endDate"], "")
        self.assertIn("startDate", record["missingFields"])
        self.assertEqual(record["verification"], "pending")

    def test_local_and_non_https_urls_rejected(self):
        for url in (
            "http://example.com",
            "https://127.0.0.1",
            "https://192.168.1.10",
            "https://[::1]",
            "https://localhost",
            "https://user:pass@example.com",
            "https://example.com:8443",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    safe_url(url)

    def test_ical_end_is_exclusive_and_nested_alarm_ignored(self):
        ical = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:unit-1
SUMMARY:Expo A and
 B
DTSTART;VALUE=DATE:20270228
DTEND;VALUE=DATE:20270302
BEGIN:VALARM
SUMMARY:Reminder
END:VALARM
LOCATION:Hall\\, Central
RRULE:FREQ=YEARLY
END:VEVENT
END:VCALENDAR"""
        page = {
            "url": SOURCE["url"],
            "body": ical,
            "headers": {"content-type": "text/calendar"},
        }
        results, _, _, errors, _ = page_records(page, SOURCE, "2026-11-18")
        self.assertEqual(errors, 0)
        event = results[0]
        self.assertEqual(event["extractionMethod"], "ical")
        self.assertEqual(
            (event["name"], event["startDate"], event["endDate"]),
            ("Expo A andB", "2027-02-28", "2027-03-01"),
        )
        self.assertEqual(event["venue"], "Hall, Central")
        self.assertIn("recurrenceNeedsReview", event["missingFields"])

    def test_microdata_nested_postal_address_and_visible_date_fallback(self):
        html = """<article itemscope itemtype="https://schema.org/BusinessEvent">
        <h2 itemprop="name">Tech <span>Expo</span></h2>
        <time itemprop="startDate" datetime="2027-01-05"></time><meta itemprop="endDate" content="2027-01-06">
        <section itemprop="location" itemscope itemtype="https://schema.org/Place"><b itemprop="name">Main Hall</b>
        <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
        <meta itemprop="addressLocality" content="Lagos"><span itemprop="addressCountry">Nigeria</span>
        </div></section></article>"""
        records, _, _, _, _ = page_records(
            {"url": SOURCE["url"], "body": html}, SOURCE, "now"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(
            (records[0]["name"], records[0]["city"], records[0]["country"]),
            ("Tech Expo", "Lagos", "Nigeria"),
        )
        self.assertEqual(records[0]["extractionMethod"], "microdata")
        found, _, hints, _, _ = page_records(
            {"url": SOURCE["url"], "body": "<p>March 4, 2027, or 5–7 April 2027</p>"},
            SOURCE,
            "now",
        )
        self.assertFalse(found)
        self.assertEqual(hints, ["March 4, 2027", "5–7 April 2027"])

    def test_discovery_respects_robots_and_exposes_external_leads_without_fetching_them(
        self,
    ):
        class Pages(FakeFetcher):
            def get(self, url, delay=None, robots=None):
                self.requests.append(url)
                if url.endswith("robots.txt"):
                    return {
                        "status": 200,
                        "url": url,
                        "body": "User-agent: *\nDisallow: /closed.ics\n",
                    }
                if url == SOURCE["url"]:
                    return {
                        "status": 200,
                        "url": url,
                        "body": """<a href="/closed.ics">Calendar</a>
                    <a href="https://another-organizer.example/events">All events</a>
                    <a href="/public.ics">Calendar</a>""",
                    }
                if url.endswith("public.ics"):
                    return {
                        "status": 200,
                        "url": url,
                        "body": "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:A\nDTSTART:20261201\nEND:VEVENT\nEND:VCALENDAR",
                    }
                raise AssertionError(url)

        fetcher = Pages("")
        status, found = collect_source(SOURCE, fetcher, "2026-11-18")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["endDate"], "2026-12-01")
        self.assertEqual(
            status["pages"][0]["externalLinksForReview"][0]["url"],
            "https://another-organizer.example/events",
        )
        self.assertEqual(
            fetcher.requests,
            [
                "https://example.com/robots.txt",
                SOURCE["url"],
                "https://example.com/public.ics",
            ],
        )
        self.assertIn("blocked", {s["status"] for s in status["pages"]})

    def test_official_unambiguous_date_formats(self):
        for literal, expected in [
            ("2027/04/13 10:00", "2027-04-13"),
            ("2026-11-9", "2026-11-09"),
            ("Mon Dec 07 06:00:00 UTC 2026", "2026-12-07"),
            ("03/04/27", ""),
            ("2027/02/30", ""),
        ]:
            self.assertEqual(
                normalize_event({"startDate": literal}, SOURCE, SOURCE["url"], "now")[
                    "startDate"
                ],
                expected,
            )

    def test_graph_location_references_across_script_tags(self):
        event = {"@type": "Event", "name": "Summit", "location": {"@id": "#venue"}}
        graph = {
            "@graph": [
                {
                    "@id": "#venue",
                    "@type": "Place",
                    "name": "Hall A",
                    "address": {"@id": "#address"},
                },
                {
                    "@id": "#address",
                    "addressCountry": "SG",
                    "addressLocality": "Singapore",
                },
            ]
        }
        html = "".join(
            '<script type="application/ld+json">' + json.dumps(node) + "</script>"
            for node in (graph, event)
        )
        record = page_records({"url": SOURCE["url"], "body": html}, SOURCE, "now")[0][0]
        self.assertEqual(
            (record["venue"], record["city"], record["country"]),
            ("Hall A", "Singapore", "SG"),
        )

    def test_human_readable_organizer_faqs(self):
        cases = [
            (
                "https://pay360event.com/faqs",
                "PAY360 21 &amp; 22 April 2027 Excel, London, UK",
                "2027-04-21",
                "2027-04-22",
                "London",
            ),
            (
                "https://india.blackswansummit.com/faqs",
                "Black Swan Summit India. When and where is the Summit? 4–5 March 2027 at Mayfair Sanctuary, Bhubaneswar, Odisha. Invitation-only on 3 March.",
                "2027-03-04",
                "2027-03-05",
                "Bhubaneswar",
            ),
        ]
        for url, passage, start, end, city in cases:
            records = page_records(
                {"url": url, "body": "<p>" + passage + "</p>"}, SOURCE, "now"
            )[0]
            self.assertEqual(
                [(c["startDate"], c["endDate"], c["city"]) for c in records],
                [(start, end, city)],
            )

    def test_calendar_sister_show_domains_are_queued_for_review(self):
        src = dict(SOURCE, sourceType="discovery")
        html = '<a href="https://sister-show.example/">Los Angeles</a><a href="https://www.linkedin.com/">LinkedIn</a>'
        records, local, hints, errors, external = page_records(
            {"url": SOURCE["url"], "body": html}, src, "now"
        )
        self.assertEqual(local, [])
        self.assertEqual(
            external, [{"url": "https://sister-show.example/", "label": "Los Angeles"}]
        )

    def test_sub_event_relationship_survives_graph_and_inline_references(self):
        parent = {
            "@type": "Event",
            "@id": "#main",
            "name": "Main Show",
            "subEvent": [{"@id": "#linked"}, {"@type": "Event", "name": "Session B"}],
        }
        linked = {"@type": "Event", "@id": "#linked", "name": "Session A"}
        explicit = {"@type": "Event", "superEvent": "#main", "name": "Session C"}
        body = json.dumps({"@graph": [parent, linked, explicit]})
        found = page_records({"url": SOURCE["url"], "body": body}, SOURCE, "now")[0]
        self.assertEqual(
            {row["name"] for row in found if row["evidence"].get("superEvent")},
            {"Session A", "Session B", "Session C"},
        )

    def test_rotation_covers_a_long_organizer_calendar(self):
        links = [str(i) for i in range(12)]
        visited = set()
        for day in range(1, 13):
            scheduled = rotate_links(links, 4, f"2026-11-{day:02d}T02:00:00Z")
            self.assertEqual(scheduled[0], links[0])
            visited.update(scheduled[:4])
        self.assertEqual(visited, set(links))

    def test_sff_extracts_expo_dates_without_invite_only_days(self):
        url = "https://www.fintechfestival.sg/festival-overview"
        body = "<h1>SFF 2026 Festival Overview</h1><p>Annual Meetings Mon 16 – Tue 17 November</p><p>Singapore FinTech Festival at Singapore EXPO Wed 18 – Fri 20 November</p>"
        results, _, _, _, _ = page_records({"url": url, "body": body}, SOURCE, "now")
        self.assertEqual(len(results), 1)
        self.assertEqual(
            (results[0]["startDate"], results[0]["endDate"]),
            ("2026-11-18", "2026-11-20"),
        )
        self.assertEqual(results[0]["missingFields"], [])
        self.assertIn("EXPO", results[0]["sourceNote"])
        no_year = body.replace(" 2026", "")
        for u, b in [
            (url, no_year),
            (SOURCE["url"], body),
            (url, body.replace("20 November", "17 November")),
        ]:
            self.assertEqual(page_records({"url": u, "body": b}, SOURCE, "now")[0], [])
        home = "https://www.fintechfestival.sg/"
        body = "<p>Singapore FinTech Festival 2027</p><p>12 - 14 November 2027 | Singapore EXPO</p>"
        self.assertEqual(
            page_records({"url": home, "body": body}, SOURCE, "now")[0][0]["startDate"],
            "2027-11-12",
        )


if __name__ == "__main__":
    unittest.main()
