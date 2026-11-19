import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import company_capture as cap


STAMP = "2026-11-19T08:00:00Z"
EVENT = {"id":"ex", "nameEn":"Identity Expo 2027", "name":"身份展", "startDate":"2027-05-01","endDate":"2027-05-03"}
URL = "https://example.org/exhibitors"
DATA = {"companies":[{"id":"test-only","name":"Example Chips", "aliases":["Example Chips AG"],"relationship":"both"},
                     {"name":"Sample Card", "aliases":[], "relationship":"peer"}]}


class CaptureTests(unittest.TestCase):
    def check(self, content, role=None, url=URL):
        return cap.extract_matches(content,url,EVENT,cap.company_index(DATA),STAMP,expected_roles=[role] if role else [])

    def test_compilation_does_not_store_names_and_rejects_ambiguous_aliases(self):
        safe=cap.compile_roster(DATA)
        self.assertNotIn("Example",json.dumps(safe))
        self.assertEqual(cap.company_index(DATA),cap.company_index(safe))
        with self.assertRaises(ValueError):
            cap.compile_roster({"companies":DATA["companies"]+[dict(DATA["companies"][0],name="Another")]})

    def test_exact_directory_organization_and_role_with_exclusions(self):
        html = """
        <title>2027 Exhibitors | Identity Expo</title><nav><li>Sample Card</li></nav>
        <main><ul><li>Example Chips AG</li><li>Sample Cardiology</li></ul>
        <article><h3>Product news</h3><p>The system supports Sample Card.</p></article></main>
        <footer><h4>Sample Card</h4></footer>"""
        rows,_,dated = self.check(html)
        self.assertTrue(dated)
        self.assertEqual([(m["name"],m["role"]) for m in rows],[("Example Chips AG","exhibitor")])

    def test_old_edition_and_wrong_show_and_past_subsection(self):
        for heading in ("2026 Exhibitors | Identity Expo","2027 Exhibitors | Other Expo","Exhibitors | Identity Expo"):
            self.assertFalse(self.check(f"<title>{heading}</title><li>Example Chips</li><footer>2027</footer>")[0])
        html="<title>2027 Sponsors | Identity Expo</title><section><h2>2026 sponsors</h2><li>Example Chips</li></section>"
        self.assertFalse(self.check(html)[0])

    def test_legal_suffixes_without_matching_siblings_or_substrings(self):
        data = {"companies":[{"name":"Example Chips","relationship":"watchlist"},
                             {"name":"Example Chips Ltd","relationship":"peer"}]}
        lookup = cap.company_index(data)
        for label in ("EXAMPLE CHIPS ASIA PACIFIC PTE. LTD.", "Example Chips Co. Ltd.",
                      "Example Chips S.A."):
            self.assertEqual(cap.recognized(label,lookup)[1:], ("watchlist", label))
        self.assertEqual(cap.recognized("Example Chips Ltd.",lookup)[1],"peer")
        for label in ("Example Chips Equipment Ltd", "Example", "Chips Ltd", "Example Chips partner"):
            self.assertIsNone(cap.recognized(label,lookup))

    def test_mixed_wall_and_previous_edition_module(self):
        mixed = "<title>Identity Expo 2027 Partners &amp; Sponsors</title><ul><li>Example Chips</li></ul>"
        self.assertFalse(self.check(mixed,"sponsor")[0])
        groups = """<title>Identity Expo 2027 Sponsors</title><main><section><h2>2026 sponsors</h2></section>
        <section><img alt="Sample Card"></section><section><h2>2027 Sponsors</h2><img alt="Example Chips"></section>
        </main>"""
        self.assertEqual([r["name"] for r in self.check(groups)[0]],["Example Chips"])

    def test_visible_section_header_overrides_widget_styles(self):
        html = """<title>Identity Expo 2027 Exhibitors and Sponsors</title><div class="p-article__wrapper-two">
        <div class="p-article__header"><h2>Exhibitors</h2></div>
        <div class="sponsors-list"><li>Example Chips</li></div></div>"""
        self.assertEqual([r["role"] for r in self.check(html)[0]],["exhibitor"])

    def test_generic_profile_name_requires_its_linked_company_website(self):
        data = {"companies":[{"name":"Example Chips","relationship":"watchlist",
                             "domainAliases":[["AC","chips.example"]]}]}
        html = """<title>2027 Exhibitors | Identity Expo</title><div class="table"><h3>Exhibitor</h3>
        <a data-toggle="modal" href="#profile1"><img alt="AC at Identity Expo 2027"></a></div>
        <div id="profile1" role="dialog" class="modal" aria-hidden="true"><a class="url" href="https://www.chips.example/"></a></div>"""
        def capture(x):
            return cap.extract_matches(x,URL,EVENT,cap.company_index(data),STAMP)[0]
        self.assertEqual([r["name"] for r in capture(html)],["AC"])
        for wrong in (html.replace("#profile1","#other"),html.replace("www.chips.example","another.example"),
                      html.replace(" at Identity Expo 2027"," at Identity Expo 2026")):
            self.assertFalse(capture(wrong))

    def test_displayed_logo_requires_organizer_directory_and_explicit_role(self):
        page = "https://hsp-asia.com/sponsors"
        body = """<title>2027 Sponsors | Identity Expo</title><div class="sponsor-card" data-sponsor-type="Gold">
        <img src="https://ri-events-media-files.s3.amazonaws.com/logos/ExampleChips.svg"></div>"""
        self.assertEqual([r["name"] for r in self.check(body,url=page)[0]],["Example Chips"])
        self.assertFalse(self.check(body,url=URL)[0])
        self.assertFalse(self.check(body.replace("s3.amazonaws.com","different.example"),url=page)[0])
        self.assertFalse(self.check(body.replace("2027 Sponsors","2026 Sponsors"),url=page)[0])

    def test_registration_popup_only_binds_matching_dates(self):
        page = "https://www.fintechconnect.com/sponsors-exhibitors"
        body = """<title>Identity Expo Sponsors</title><li>Example Chips</li><div class="ftcp" role="dialog" aria-hidden="true">
        <div class="ftcp-eyebrow">1–3 May 2027 · London</div></div>"""
        self.assertTrue(self.check(body,url=page)[0])
        self.assertFalse(self.check(body.replace("1–3","3–5"),url=page)[0])

    def test_mixed_roles_need_an_entry_level_badge(self):
        html = """<title>Sponsors and Exhibitors 2027 | Identity Expo</title><li>Sample Card</li>
        <li><a class="sff26-exl__a"><span class="sff26-exl__badge">Sponsor</span>
        <span class="sff26-exl__n">Example Chips</span></a></li>"""
        rows,_,_=self.check(html)
        self.assertEqual({(m["name"],m["role"]) for m in rows},
                         {("Example Chips","sponsor"),("Example Chips","exhibitor")})

    def test_attendee_localized_names_and_confirmed_dates(self):
        for label, role in [("Attendee Companies","attendee"),("Exposants","exhibitor"),("Aussteller","exhibitor"),
                             ("Patrocinadores","sponsor")]:
            rows,_,_=self.check(f"<title>{label} | Identity Expo</title><p>May 1–3, 2027</p><li>Example Chips</li>")
            self.assertEqual([r["role"] for r in rows],[role])
        self.assertFalse(self.check("<title>Exhibitors | Identity Expo</title><p>March 1–3, 2027</p><li>Example Chips</li>")[0])

    def test_schema_organization_is_never_the_website_publisher(self):
        obj={"@type":"Event","name":"Identity Expo 2027","startDate":"2027-05-01","sponsor":{"@type":"Organization","name":"Example Chips"},
             "publisher":{"@type":"Organization","name":"Sample Card"}}
        rows,_,_=self.check('<script type="application/ld+json">'+json.dumps(obj)+'</script>')
        self.assertEqual([(r["name"],r["role"]) for r in rows],[("Example Chips","sponsor")])

    def test_edition_logo_in_local_role_group(self):
        html = """<title>Identity Expo 2027 | sponsors and exhibitors</title><div class="table">
        <h3>Gold Sponsor</h3><img alt="Example Chips at Identity Expo 2027"></div>
        <div class="table"><h3>Startup Plus</h3><img alt="Sample Card at Identity Expo 2027"></div>
        <div class="table"><h3>Exhibitor</h3><img alt="Sample Card at Identity Expo 2026"></div>"""
        self.assertEqual([(m["name"],m["role"]) for m in self.check(html)[0]],[("Example Chips","sponsor")])

    def test_current_french_exhibitor_list_requires_its_intro(self):
        url = "https://www.trustech-event.com/fr-FR/campaigns/vis-ga-gen"
        event = dict(EVENT,id="v-trustech-2026",nameEn="TRUSTECH 2026",startDate="2026-12-01",endDate="2026-12-03")
        body = "<p>Plus de 200 entreprises exposeront sur TRUSTECH 2026, dont :</p><p>Example Chips ● Sample Card ● Another</p>"
        def capture(x):
            return cap.extract_matches(x,url,event,cap.company_index(DATA),STAMP)[0]
        self.assertEqual({(m["name"],m["role"]) for m in capture(body)},
                         {("Example Chips","exhibitor"),("Sample Card","exhibitor")})
        self.assertFalse(capture(body.replace("2026","2025")))
        self.assertFalse(capture(body.replace("exposeront","ont exposé")))

    def test_lightweight_discovery_including_navigation(self):
        html="""<title>Identity Expo 2027</title><nav><a href="/2027/sponsors">2027 Sponsors</a>
        <a href="https://external.example.org/sponsors">Sponsors</a><a href="/2025/exhibitors">Past exhibitors</a></nav>"""
        rows,links,_=self.check(html)
        self.assertEqual([x[0] for x in links],["https://example.org/2027/sponsors"])
        self.assertFalse(rows)

    def test_status_clears_removed_names_but_preserves_failed_check(self):
        data=cap.compile_roster(DATA)
        first=self.check("<title>Exhibitors 2027 | Identity Expo</title><li>Example Chips</li>")[0]
        snap={"rosterDigest":cap.digest(json.dumps(data,sort_keys=True)),"checkedAt":STAMP,
              "pages":[dict(eventId="ex",url=URL,state="checked",matches=first)]}
        event=dict(EVENT)
        cap.apply_snapshot([event],data,snap,STAMP)
        self.assertEqual(event["companyMatches"],first)
        cap.apply_snapshot([event],data,snap,STAMP)
        self.assertEqual(event["companyMatches"],first)
        snap["pages"][0].update(state="unavailable",matches=[])
        cap.apply_snapshot([event],data,snap,STAMP)
        self.assertEqual(event["companyMatches"],first)
        snap["pages"][0].update(state="checked")
        cap.apply_snapshot([event],data,snap,STAMP)
        self.assertEqual(event["companyMatches"],[])

    def test_pagination_is_restricted_to_same_origin_directory_links(self):
        d=cap.Directory('<nav><a href="?page=2">2</a><a rel="next" href="/exhibitors/page/3">Next page</a>'
                        '<a rel="next" href="https://other.example/page/4">Next</a><a href="/booth">24</a></nav>')
        self.assertEqual(set(cap.pagination_links(d,URL)),{URL+"?page=2",URL+"/page/3"})

    def test_absent_or_empty_configuration_causes_no_network_or_data_changes(self):
        with TemporaryDirectory() as temp, patch.object(cap.collector,"Fetcher",side_effect=AssertionError):
            root=Path(temp)
            self.assertIsNone(cap.scan(root,root,STAMP))
            (root/"company-watchlist.json").write_text('{"companies":[]}')
            self.assertIsNone(cap.scan(root,root,STAMP))
            self.assertFalse((root/"company-evidence.json").exists())
        old=dict(EVENT,companyMatches=[{"name":"old"}])
        cap.apply_snapshot([old],{"companies":[]},None,STAMP)
        self.assertEqual(old["companyMatches"],[{"name":"old"}])


if __name__ == "__main__":
    unittest.main()
