from copy import deepcopy
from datetime import date, datetime, timezone
import csv
import io
import json
import shutil
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from zipfile import ZipFile

import refresh

STAMP = "2026-11-18T09:00:00Z"
TODAY = date(2026, 11, 18)
END = date(2027, 6, 30)


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ZipFile(Path(__file__).with_name("exhibition-source.zip")) as z:
            events = json.loads(z.read(refresh.PREFIX + "data/verified-events.json"))["events"]
        cls.original = next(e for e in events if e["id"] == "live-sff-2026")

    def setUp(self):
        self.old = deepcopy(self.original)
        self.source = {"id":"sg", "name":"新加坡金融科技节", "nameEn":"Singapore FinTech Festival 2026",
                       "url":"https://www.fintechfestival.sg/festival-overview", "official":True,
                       "enabled":True, "topics":["fintech"]}
        self.run = {"state":"completed", "sourceCount":1, "lastRunAt":STAMP, "sources":[
            {"id":"sg", "status":"success", "checkedAt":STAMP}]}
        self.c = {"id":"c", "extractionMethod":"jsonld", "name":"Singapore FinTech Festival 2026",
                  "country":"Singapore", "city":"Singapore", "startDate":"2026-11-18","endDate":"2026-11-20",
                  "sourceId":"sg", "sourceUrl":self.source["url"], "collectedAt":STAMP,
                  "eventStatus":"EventScheduled", "evidence":{"startDate":"2026-11-18"}}

    def cycle(self,*c):
        return refresh.reconcile([self.old], {"sources":[self.source]}, self.run, c, TODAY, END)

    def test_new_verified_edition_gets_company_coverage_in_same_run(self):
        from company_capture import read_catalog
        event = dict(self.c, name="Tokyo Payment Expo", city="Tokyo", country="JP",
                     startDate="2027-02-05", endDate="2027-02-06",
                     sourceUrl="https://www.fintechfestival.sg/tokyo-payment-expo")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(Path(__file__).with_name("exhibition-source.zip"), root / "exhibition-source.zip")
            (root / "sources.json").write_text(json.dumps({"sources":[self.source]}))
            (root / "collection-status.json").write_text(json.dumps(self.run))
            (root / "review-queue.json").write_text(json.dumps({"candidates":[event]}))
            names = {e["nameEn"] for e in read_catalog(root, root, STAMP)}
        self.assertIn(event["name"], names)

    def test_ongoing_includes_start_and_last_day(self):
        self.assertTrue(refresh.in_window(self.old, TODAY, END))
        self.assertTrue(refresh.in_window(self.old, date(2026, 11, 20), END))
        self.assertFalse(refresh.in_window(self.old, date(2026, 11, 21), END))
        self.assertFalse(refresh.in_window(dict(self.old, startDate="2027-07-01", endDate="2027-07-02"), TODAY, END))

    def test_expires_at_midnight_in_each_host_city(self):
        moment = datetime(2026, 11, 20, 19, tzinfo=timezone.utc)
        self.assertFalse(refresh.in_window(self.old, moment, END))
        la = dict(self.old, countryEn="United States", cityEn="Los Angeles")
        la.pop("timeZone", None)
        self.assertTrue(refresh.in_window(la, moment, END))
        self.assertFalse(refresh.in_window(la, datetime(2026, 11, 21, 8, tzinfo=timezone.utc), END))
        unknown = dict(la, cityEn="Unknown municipality")
        self.assertEqual(refresh.event_zone(unknown), "Etc/GMT+12")
        self.assertTrue(refresh.in_window(unknown, datetime(2026, 11, 21, 11, tzinfo=timezone.utc), END))

    def test_confirmation_preserves_curated_identity(self):
        events, logs = self.cycle(self.c)
        self.assertEqual(logs[0]["action"],"confirmed")
        self.assertEqual(events[0]["id"],self.old["id"])
        self.assertEqual(events[0]["name"],self.old["name"])
        self.assertEqual(events[0]["descriptionEn"],self.old["descriptionEn"])

    def test_single_schema_date_change_stays_pending_like_pay360(self):
        item = dict(self.c, startDate="2026-12-18", endDate="2026-12-20")
        rows, log = self.cycle(item, dict(item, sourceUrl=item["sourceUrl"]+"?edition=2026"),
                               dict(item, sourceUrl="https://www.fintechfestival.sg/footer-copy"))
        self.assertEqual(rows, [self.old])
        self.assertEqual({d["reason"] for d in log}, {"date_change_needs_corroboration"})

    def test_two_date_sources_or_specific_schedule_can_correct(self):
        item = dict(self.c, startDate="2026-12-18", endDate="2026-12-20")
        for batch in ([item, dict(item, extractionMethod="ical", sourceUrl="https://www.fintechfestival.sg/programme.ics")],
                      [dict(item, extractionMethod="organizer_schedule", evidence={"sourcePassage":"18–20 December 2026"})]):
            rows, log = self.cycle(*batch)
            self.assertEqual(rows[0]["startDate"],"2026-12-18")
            self.assertEqual(log[0]["action"],"updated")

    def test_conflicting_dates_keep_old_record(self):
        rows, log = self.cycle(self.c, dict(self.c, endDate="2026-11-21"))
        self.assertEqual(rows, [self.old])
        self.assertEqual({e["action"] for e in log}, {"pending"})

    def test_cancellation_and_explicit_reinstatement(self):
        rows, _ = self.cycle(dict(self.c, eventStatus="https://schema.org/EventCancelled"))
        self.assertFalse(refresh.in_window(rows[0], TODAY, END))
        self.old = rows[0]
        rows, log = self.cycle(dict(self.c, eventStatus=""))
        self.assertEqual(log[0]["reason"], "reinstatement_unconfirmed")
        rows, log = self.cycle(self.c)
        self.assertTrue(refresh.in_window(rows[0], TODAY, END))

    def test_postponed_to_already_confirmed_dates_stays_visible(self):
        c = dict(self.c, eventStatus="EventPostponed", description="Postponed to 18–20 November 2026")
        rows, log = self.cycle(c)
        self.assertEqual(log[0]["action"], "pending")
        self.assertTrue(refresh.in_window(rows[0], TODAY, END))
        rows, log = self.cycle(dict(c, description="Postponed until further notice"))
        self.assertFalse(refresh.in_window(rows[0], TODAY, END))

    def test_new_edition_uses_location_and_stable_ids(self):
        new = dict(self.c, name="Japan Banking Expo 2027", city="Tokyo", country="JP",
                   startDate="2027-05-20", endDate="2027-05-23")
        rows, log = self.cycle(new, new)
        self.assertEqual([d["action"] for d in log], ["added","duplicate"])
        self.assertEqual((rows[-1]["country"], rows[-1]["topics"]), ("日本",["banking"]))
        self.assertEqual(len({e["id"] for e in rows}), 2)
        repeat, log = refresh.reconcile(rows, {"sources":[self.source]}, self.run, [new], TODAY, END)
        self.assertEqual([e["id"] for e in repeat], [e["id"] for e in rows])
        self.assertEqual(log[0]["action"],"confirmed")

    def test_partial_source_or_stale_candidate_keeps_catalog(self):
        self.run["sources"][0]["status"] = "error"
        rows, log = self.cycle(self.c)
        self.assertEqual(rows,[self.old])
        self.assertEqual(log[0]["reason"],"not_current")
        self.run["sources"][0]["status"] = "success"
        rows, log = self.cycle(dict(self.c,collectedAt="2026-11-17T09:00:00Z"))
        self.assertEqual(rows,[self.old])

    def test_unrelated_or_external_or_unlocated_is_held(self):
        for change in [dict(name="Global Music Expo",startDate="2027-05-20",endDate="2027-05-23"),
                       dict(recurrenceNeedsReview=True), dict(country=""), dict(sourceUrl="https://example.org/calendar"),
                       dict(eventUrl="javascript:alert(1)"), dict(startDate="2026-11-31")]:
            rows, log = self.cycle(dict(self.c, **change))
            self.assertEqual(rows,[self.old])
            self.assertEqual(log[0]["action"],"pending")

    def test_standalone_impact_day_from_official_directory(self):
        self.source.update(url="https://www.kuppingercole.com/events", sourceType="discovery")
        event = dict(self.c, name="Nordics Identity Impact Day 2027", city="Gothenburg", country="Sweden",
                     sourceUrl=self.source["url"], eventUrl=self.source["url"]+"/niid2027",
                     startDate="2027-02-09", endDate="2027-02-09")
        rows, log = self.cycle(event, dict(event, sourceUrl=event["eventUrl"]))
        self.assertEqual([d["action"] for d in log], ["added", "duplicate"])
        self.assertEqual((rows[-1]["name"], rows[-1]["city"], rows[-1]["sourceUrl"]),
                         ("北欧数字身份主题日", "哥德堡", event["eventUrl"]))
        _, log = self.cycle(dict(event, eventUrl=self.source["url"]))
        self.assertEqual(log[0]["action"], "pending")

    def test_overlapping_child_events_with_unique_paths_need_review(self):
        one = dict(self.c, name="Japan Banking Expo", city="Tokyo", country="JP", sourceUrl="https://www.fintechfestival.sg/jp/main")
        two = dict(one, name="Banking Finance Forum", sourceUrl="https://www.fintechfestival.sg/jp/forum")
        rows, log = self.cycle(one, two)
        self.assertEqual(rows, [self.old])
        self.assertEqual([d["reason"] for d in log], ["possible_duplicate", "possible_duplicate"])
        rows, log = self.cycle(dict(one, evidence={"superEvent":{"name":"Parent expo"}}))
        self.assertEqual(log[0]["reason"], "subevent_or_session")

    def test_colocated_subshow_never_becomes_separate_listing(self):
        self.source["partOfEventId"] = self.old["id"]
        rows, log = self.cycle(dict(self.c,name="LOUPE Asia Packaging Expo"))
        self.assertEqual(rows,[self.old])
        self.assertEqual(log[0]["reason"],"part_of_listed_fair")

    def test_incomplete_source_manifest_is_rejected(self):
        self.run["sourceCount"] = 12
        with self.assertRaises(ValueError):
            self.cycle(self.c)

    def test_replaying_successful_run_retains_original_release_and_audit(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy(refresh.ROOT / "exhibition-source.zip", root)
            workflow = ".github/workflows/collect.yml"
            (root / workflow).parent.mkdir(parents=True)
            shutil.copy(refresh.ROOT / workflow, root / workflow)
            (root / "sources.json").write_bytes(refresh.json_bytes({"sources": [self.source]}))
            run_dir = root / "run"
            run_dir.mkdir()
            finished = "2026-11-18T09:05:32Z"
            self.run["completedAt"] = finished
            (run_dir / "collection-status.json").write_bytes(refresh.json_bytes(self.run))
            new = dict(self.c, name="Japan Banking Expo 2027", city="Tokyo", country="JP",
                       startDate="2027-01-15", endDate="2027-01-16")
            (run_dir / "review-queue.json").write_bytes(refresh.json_bytes({"candidates": [new]}))
            one = refresh.build(root, run_dir, TODAY)
            outputs = {n: (root/n).read_bytes() for n in refresh.RELEASE}
            two = refresh.build(root, run_dir, TODAY)
            self.assertEqual(one, two)
            self.assertEqual(one["decisions"].get("added"), 1)
            self.assertTrue(all((root/n).read_bytes() == data for n, data in outputs.items()))
            refresh.build(root, None, TODAY)
            self.assertTrue(all((root/n).read_bytes() == data for n, data in outputs.items()))
            for name in ("events-en.csv", "events-zh.csv"):
                rows = list(csv.reader(io.StringIO(outputs[name].decode("utf-8-sig"))))
                self.assertTrue(all(row[-1] == finished for row in rows[1:]))
            self.run["completedAt"] = "2026-11-18T11:05:32Z"
            (run_dir / "collection-status.json").write_bytes(refresh.json_bytes(self.run))
            refresh.build(root, run_dir, TODAY)
            with ZipFile(root / "exhibition-source.zip") as archive:
                status = json.loads(archive.read(refresh.PREFIX + "data/collection-status.json"))
                self.assertEqual(status["dataUpdatedAt"], self.run["completedAt"])

    def test_only_precise_finished_instants_can_label_an_update(self):
        self.assertEqual(refresh.completion_stamp({"lastRunAt": STAMP}), "")
        for wrong in ("2026-11-18", "2026-11-18T08:59:59Z"):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                refresh.completion_stamp({"lastRunAt": STAMP, "dataUpdatedAt": wrong})

    def test_only_confirmed_company_names_are_exported_with_official_spellings(self):
        match = dict(companyId="confirmed", name="Example Singapore Pte Ltd", relationship="peer",
                     role="exhibitor", checkedAt="2026-11-19", sourceUrl="https://example.org/exhibitors")
        event = dict(self.old, companyMatches=[match])
        available = {"confirmed": {"zh":"示例公司", "en":"Example", "priority":"P2"},
                     "unconfirmed": {"zh":"其他企业", "en":"Other"}}
        labels = refresh.confirmed_labels([event], available)
        self.assertEqual(labels, {"confirmed": {"zh":"示例公司", "en":"Example"}})
        for en, expected in [(False,"示例公司"),(True,"Example")]:
            with self.subTest(en=en):
                cell = list(csv.reader(io.StringIO(refresh.csv_export([event],en,labels).decode("utf-8-sig"))))[1][-2]
                self.assertIn(f"{expected} ({match['name']})", cell)
                self.assertIn(match["sourceUrl"], cell)

    def test_spreadsheet_and_inline_script_escape_untrusted_content(self):
        e = dict(self.old, nameEn="=2+5", descriptionEn="</script><script>alert(1)</script>")
        cell = list(csv.reader(io.StringIO(refresh.csv_export([e], True).decode("utf-8-sig"))))[1][0]
        self.assertEqual(cell,"'=2+5")
        bundle = refresh.make_bundle({}, [e], "", {})
        self.assertNotIn(b"<script>", bundle)
        self.assertEqual(json.loads(bundle.decode().split("=",1)[1].strip().rstrip(";"))["events"][0], e)


if __name__ == "__main__":
    unittest.main()
