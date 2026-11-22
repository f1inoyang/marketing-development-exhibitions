"""Match a company's listed name in the current edition's official participation evidence."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

import collector


ROLES = {
    "exhibitor": r"\bexhibitors?\b|\bexhibiting companies\b|参展商|展商名录|\b(?:exposants?|expositores?|aussteller|expositores)\b",
    "sponsor": r"\bsponsors?(?:hip)?\b|赞助商|\b(?:patrocinadores?|patrocinadores|sponsoren|commanditaires?)\b",
    "speaker": r"\bspeakers?\b|\b(?:ponentes|palestrantes|conférenciers|referenten)\b|演讲嘉宾|演讲者",
    "attendee": r"\battend(?:ee|ing) (?:companies|organi[zs]ations)\b|参会机构|参会企业",
    "organizer": r"\borgan[izs]ers?\b|\borgani[sz]ed by\b|主办方",
    "partner": r"\b(?:official|event|festival|conference) partners?\b|\bpartners?\s*(?:&|and|et)\s*sponsors?\b|\bsponsors?\s*(?:&|and|et)\s*partners?\b|官方合作伙伴|大会合作伙伴",
}
HISTORIC = re.compile(r"\b(?:previous|past|former|last year's|recap|highlights from|alumni)\b|往届|上届|历届|\b(?:ant[eé]rieur(?:e|es|s)?|pr[eé]c[eé]dent(?:e|es|s)?|anteriores?|passad[oa]s?|vergangene[snr]?|vorjahr)\b", re.I)
YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
SKIP = set("script style template noscript footer form select svg".split())
RELATIONS = {"peer", "supplier", "watchlist", "both"}
HEADINGS = {f"h{i}" for i in range(1, 7)}
DIRECT_NAME = re.compile(r"(?:company|organisation|organization|exhibitor|sponsor|partner|employer).{0,18}(?:name|title)|speaker.{0,12}(?:company|employer)", re.I)
MAX_DIRECTORY_LINKS = 5
MAX_PAGINATION_PAGES = 3


def normalized(s):
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    return " ".join(re.findall(r"[^\W_]+", s, re.UNICODE))


def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()


def spellings(name):
    """Accept spacing/punctuation variants, never substrings or fuzzy brand names."""
    return normalized(re.sub(r"\s+(?:logo|标志)$", "", name, flags=re.I).strip())


def domain_signature(name, host):
    host = host.lower().removeprefix("www.").rstrip(".")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+",host):
        raise ValueError("Invalid organisation website domain")
    return digest(spellings(name) + "\n" + host)


def compile_roster(data):
    groups = []
    owners = {}
    for c in data["companies"]:
        name = c["name"].strip()
        relation = c["relationship"]
        if not name or relation not in RELATIONS:
            raise ValueError("A company needs a display name and a recognized relationship")
        aliases = {spellings(x) for x in [name, *c.get("aliases", [])] if x.strip()}
        signatures = {digest(a) for a in aliases if a}
        signatures.update(domain_signature(name,host) for name,host in c.get("domainAliases",[]))
        hashes = sorted(signatures)
        if not hashes or any(h in owners for h in hashes):
            raise ValueError("Two companies share an alias; resolve the ambiguous name first")
        cid = "co-" + digest(normalized(name))[:20]
        for h in hashes:
            owners[h] = cid
        groups.append({"id": cid, "signatures": hashes, "relationship": relation})
    return {"version": 1, "companies": sorted(groups, key=lambda c: c["id"])}


def company_index(data):
    if any("name" in c for c in data["companies"]):
        data = compile_roster(data)
    lookup = {}
    ids = set()
    for c in data["companies"]:
        cid, rel = c["id"], c["relationship"]
        if not re.fullmatch(r"co-[a-f0-9]{20}", cid) or cid in ids or rel not in RELATIONS:
            raise ValueError("Invalid compiled company ID or relationship")
        ids.add(cid)
        for sig in c["signatures"]:
            if not re.fullmatch(r"[0-9a-f]{64}", sig) or sig in lookup:
                raise ValueError("Ambiguous or invalid company signature")
            lookup[sig] = (cid, rel)
    return lookup


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.parts = []
        self.children = []

    def short(self):
        # A large structural block can never be one company's name.
        if len(self.parts) > 30:
            return ""
        parts = []
        for p in self.parts:
            value = p.short() if isinstance(p, Node) else p
            if not value:
                continue
            parts.append(value)
            if sum(map(len, parts)) > 235:
                return ""
        return " ".join(" ".join(parts).split())

    def ancestors(self):
        p = self
        while p:
            yield p
            p = p.parent


class Directory(HTMLParser):
    def __init__(self, body, profile_links=False):
        super().__init__(convert_charrefs=True)
        self.profile_links = profile_links
        self.root = self.current = Node("root", [], None)
        self.nodes = []
        self.skip = []
        self.depth = 0
        self.feed(body)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skip:
            if tag not in VOID:
                self.skip.append(tag)
            return
        directory_profile = self.profile_links and (
            (attrs.get("role") == "dialog" and {"ftcp","modal"}.intersection(str(attrs.get("class") or "").split()))
            or "id4a-ex-data" in str(attrs.get("class") or "").split())
        hidden = "hidden" in attrs or attrs.get("aria-hidden") == "true"
        if tag in SKIP or (hidden and not directory_profile) or attrs.get("role") == "contentinfo":
            if tag not in VOID:
                self.skip.append(tag)
            return
        if self.depth > 180 or len(self.nodes) > 75000:
            raise ValueError("Directory exceeds parser limits")
        n = Node(tag, attrs.items(), self.current)
        self.current.parts.append(n)
        self.current.children.append(n)
        self.nodes.append(n)
        if tag not in VOID:
            self.current = n
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                while self.skip.pop() != tag:
                    pass
            return
        p = self.current
        while p is not self.root and p.tag != tag:
            p = p.parent
        if p is not self.root:
            while self.current != p.parent:
                self.depth -= 1
                self.current = self.current.parent

    def handle_data(self, text):
        if not self.skip and text.strip():
            self.current.parts.append(text)


def roles_in(s):
    return {k for k, pattern in ROLES.items() if re.search(pattern, s, re.I)}


def is_old(s, year):
    years = set(YEAR.findall(s))
    return bool(HISTORIC.search(s) or (years and years != {year}))


def identifies(text, event, aliases):
    text = " " + normalized(text) + " "
    for alias in [event["nameEn"], event.get("name", ""), *aliases]:
        phrase = normalized(YEAR.sub("", alias)).strip()
        if phrase and " " + phrase + " " in text:
            return True
    return False


def checked_url(url):
    return collector.safe_url(url)


def page_identity(d, url, event, aliases, from_dated_link=False, body=None):
    year = event["startDate"][:4]
    title = " ".join(n.short() for n in d.nodes if n.tag == "title")
    hero = [n.short() for n in d.nodes if n.tag == "h1"]
    headings = [s for s in [title, *hero] if s]
    if is_old(title, year) or any(is_old(s, year) for s in hero[:2]):
        return False
    if not identifies(" ".join(headings) + " " + urlsplit(url).path, event, aliases) and not from_dated_link:
        return False
    # A precise published date range also binds yearless directory headings.
    start, end = (date.fromisoformat(event[k]) for k in ("startDate","endDate"))
    month = start.strftime("%B")
    if start.month == end.month and start.year == end.year:
        months = rf"(?:{month}|{month[:3]}\.?)"
        days = str(start.day) if start == end else rf"{start.day}\s*(?:[-–—]|to)\s*{end.day}"
        period = rf"(?<!\d)(?:{days}\s+{months}|{months}\s+{days})[,]?\s+{start.year}(?!\d)"
        for n in d.nodes:
            if n.tag in ("p","time","span") and len(n.children)<3 and re.search(period,n.short(),re.I):
                if not any(a.tag in ("nav","aside") for a in n.ancestors()):
                    return True
        # The registration dialog used on FinTech Connect's sponsor directory
        # displays the actual dates and venue, independent of the yearless title.
        if body and urlsplit(url).hostname == "www.fintechconnect.com" and "ftcp-eyebrow" in body:
            for n in Directory(body,profile_links=True).nodes:
                if ("ftcp-eyebrow" in str(n.attrs.get("class") or "").split() and
                        any(a.attrs.get("role")=="dialog" and "ftcp" in str(a.attrs.get("class") or "").split()
                            for a in n.ancestors()) and re.search(period,n.short(),re.I)):
                    return True
    # An organizer's distinctive abbreviated edition (e.g. MWC27 Barcelona).
    if any(re.search(rf"\b[A-Z]{{2,8}}{year[2:]}\b", s) for s in headings) and any(
        identifies(s,event,aliases) for s in headings
    ):
        return True
    # Do not use the copyright year, file names, general body text or footer.
    return (any(year in YEAR.findall(s) and not is_old(s, year) for s in headings)
            or (year in YEAR.findall(urlsplit(url).path) and not is_old(urlsplit(url).path, year))
            or from_dated_link)


def directory_links(d, base, event, aliases, dated):
    year = event["startDate"][:4]
    found = {}
    for n in d.nodes:
        if n.tag != "a" or not n.attrs.get("href"):
            continue
        text = n.short()
        raw = n.attrs["href"]
        if raw.startswith(("#","?","mailto:")):
            continue
        try:
            url = checked_url(urljoin(base,raw))
            if collector.origin(url) != collector.origin(base):
                continue
        except ValueError:
            continue
        path = urlsplit(url).path
        roles = roles_in(text+" "+path.replace("-"," ").replace("/"," "))
        if (not roles or is_old(text+" "+path,year) or
                re.search(r"\b(?:become|apply|inquiry|prospectus|opportunities|benefits|policy|media kit)\b", text, re.I)):
            continue
        direct = dated and year in YEAR.findall(text+" "+path) and identifies(text+" "+path,event,aliases)
        found[url] = (url, tuple(sorted(roles)), direct)
    return sorted(found.values(), key=lambda x: (not x[2],len(x[0]),x[0]))


def pagination_links(d, base):
    found = set()
    for n in d.nodes:
        if n.tag != "a" or not n.attrs.get("href"):
            continue
        label = " ".join((str(n.attrs.get("aria-label") or ""), n.short())).strip()
        rel = str(n.attrs.get("rel") or "").split()
        if "next" not in rel and not re.fullmatch(r"(?:page\s+)?\d+|next(?: page)?|suivant|siguiente|weiter|下一页", label, re.I):
            continue
        try:
            url = checked_url(urljoin(base, n.attrs["href"]))
            if collector.origin(url) != collector.origin(base) or url == base:
                continue
            # Only traverse an explicit next/listing page, never numbered company links.
            if "next" not in rel and not re.search(r"[?&](?:page|paged|p)=\d+|/page/\d+", url, re.I):
                continue
            found.add(url)
        except ValueError:
            pass
    return sorted(found)


def section_info(n, year, page_roles=()):
    """Look only at a candidate's own group/section, never at an unrelated preceding block."""
    headings = set()
    styles = set()
    for ancestor in n.ancestors():
        if ancestor.tag in ("body","main","html","root"):
            break
        attrs = " ".join(str(ancestor.attrs.get(k) or "") for k in ("id","class","aria-label"))
        for h in ancestor.children:
            if "p-article__header" in str(h.attrs.get("class") or "").split():
                if is_old(h.short(),year):
                    return set(), True
                headings |= roles_in(h.short())
            if h.tag not in HEADINGS or h is n:
                continue
            heading = h.short()
            if is_old(heading, year):
                return set(), True
            headings |= roles_in(heading)
        # Ignore generic footer/partners strips implemented as a div instead of a footer.
        if re.search(r"footer|cookie|payment-method|social-links|breadcrumb|navigation", attrs, re.I):
            return set(), True
        if is_old(attrs.replace("-"," "), year):
            return set(), True
        styles |= roles_in(attrs.replace("-"," ").replace("_"," "))
    # Some organiser platforms reuse their exhibitor-list CSS to present
    # sponsors. A visible section or unambiguous page heading takes precedence.
    return headings or (set(page_roles) if len(page_roles)==1 else styles),False


def recognized(raw, lookup, host=None):
    raw = " ".join(raw.split()).strip()
    unadorned = re.sub(r"\s+(?:logo|标志)$", "", raw, flags=re.I).strip()
    if not 2 <= len(unadorned) <= 150:
        return None
    spelling = spellings(unadorned)
    value = lookup.get(digest(spelling))
    if not value and host:
        value = lookup.get(domain_signature(unadorned,host))
    if not value:
        # Directly listed subsidiaries often append a legal form, e.g.
        # "Infineon Technologies Asia Pacific Pte Ltd". Only relax a terminal
        # legal form. A division, a product or a parent's name is not an alias.
        suffix = re.search(
            r" (?:(?:asia pacific )?pte (?:ltd|limited)|(?:co|company|pty) (?:ltd|limited)|"
            r"gmbh(?: co kg)?|s a s|s a|s r l|l l c|ltd|limited|inc|incorporated|"
            r"llc|corp|corporation|plc|ag|sarl|sas|sa|srl|bv)$", spelling
        )
        if suffix and len(spelling[:suffix.start()].replace(" ", "")) >= 4:
            value = lookup.get(digest(spelling[:suffix.start()]))
    return (*value, unadorned) if value else None


def roster_card_roles(n):
    # Explicit badges connect multiple roles to this one directory profile.
    for a in n.ancestors():
        cls = str(a.attrs.get("class") or "")
        if a.tag=="a" and re.search(r"\bsff\d{2}-exl__a\b",cls):
            return {"exhibitor"} | {role for child in a.children
                                    if re.search(r"\bsff\d{2}-exl__badge\b",str(child.attrs.get("class") or ""))
                                    for role in roles_in(child.short())}
        if a.attrs.get("data-sponsor-type"):
            raw = str(a.attrs["data-sponsor-type"])
            if re.fullmatch(r"diamond|gold|silver|platinum|bronze", raw, re.I):
                return {"sponsor"}
            return roles_in(raw)
        if re.search(r"(?:^|\s)[\w-]*exhibitor-profile(?:\s|$)", cls):
            pending, roles = list(a.children), set()
            for _ in range(120):
                if not pending:
                    break
                child = pending.pop()
                if "badge" in str(child.attrs.get("class") or ""):
                    roles.update(roles_in(child.short()))
                pending.extend(child.children)
            return roles
    return set()


def edition_logo(n, event, aliases):
    """Terrapinn labels logo images 'Company at Event YYYY' under a local role heading."""
    if n.tag != "img":
        return None
    label = str(n.attrs.get("alt") or "")
    company, separator, edition = label.rpartition(" at ")
    if not separator or YEAR.findall(edition) != [event["startDate"][:4]] or not identifies(edition,event,aliases):
        return None
    for ancestor in n.ancestors():
        if ancestor.tag == "div" and "table" in str(ancestor.attrs.get("class") or "").split():
            headers = [child.short() for child in ancestor.children if child.tag in HEADINGS]
            if len(headers) == 1:
                roles = roles_in(headers[0])
                if len(roles) == 1:
                    return company, roles.pop()
            break
    return None


def organizer_logo(n, url, lookup):
    """ReConnaissance directories identify some companies only by the displayed logo."""
    if urlsplit(url).hostname not in {"hsp-asia.com","hsp-emea.com","opticaldigitalsecurity.com"}:
        return None
    if n.tag != "img" or n.attrs.get("alt") or not roster_card_roles(n):
        return None
    source = re.fullmatch(r"https://ri-events-media-files\.s3\.amazonaws\.com/logos/([\w-]{4,90})\.(?:svg|png|jpg)",
                          str(n.attrs.get("src") or ""),re.I)
    if not source:
        return None
    literal = source.group(1)
    if re.fullmatch(r"[a-z]{1,3}[A-Z]{1,3}",literal):
        return None
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])"," ",literal).replace("-"," ")
    result = recognized(literal,lookup) or recognized(spaced,lookup)
    if result:
        return result
    # One logo can carry a company's parent name and trading name. Both must
    # independently resolve to the same roster ID before accepting the card.
    parts = literal.split("-")
    for i in range(1,len(parts)):
        left = recognized(" ".join(parts[:i]),lookup)
        right = recognized(" ".join(parts[i:]),lookup)
        if left and right and left[0] == right[0]:
            return (*left[:2]," ".join(parts))
    return None


def exhibitor_websites(body):
    # Inline profiles are hidden until a visitor clicks a specific directory
    # tile. Only accept the website field of the exact linked exhibitor profile.
    if "id4a-name-trigger" not in body and 'data-toggle="modal"' not in body:
        return {}
    profiles = defaultdict(set)
    for link in Directory(body,profile_links=True).nodes:
        if link.tag != "a":
            continue
        classes = str(link.attrs.get("class") or "").split()
        if "url" not in classes and "id4a-m-web" not in classes:
            continue
        href = str(link.attrs.get("href") or "")
        try:
            parsed = urlsplit(href)
            host = parsed.hostname if parsed.scheme in ("http","https") else None
        except ValueError:
            continue
        if not host or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+",host):
            continue
        for ancestor in link.ancestors():
            profile_id = ancestor.attrs.get("id")
            classes = str(ancestor.attrs.get("class") or "").split()
            if profile_id and ("id4a-ex-data" in classes or
                    ("modal" in classes and ancestor.attrs.get("role") == "dialog")):
                profiles[profile_id].add(host)
                break
    return {profile_id: hosts.pop() for profile_id,hosts in profiles.items() if len(hosts)==1}


def trustech_directory(d, event, url):
    """The French organiser publishes a bullet-separated list inside a paragraph."""
    if "trustech" not in normalized(event["nameEn"]).split() or urlsplit(url).hostname not in (
            "www.trustech-event.com", "trustech-event.com"):
        return []
    year = event["startDate"][:4]
    intro = re.compile(rf"\bexposeront\s+sur\s+TRUSTECH\s+{year}\s*,?\s+dont\s*:", re.I)
    paragraphs = []
    for n in d.nodes:
        if n.tag not in {"p","li","div"} or any(a.tag in {"aside","nav","header"} for a in n.ancestors()):
            continue
        # A paragraph may contain nested styling or links. Never span from one
        # content section into another when assembling the organisation names.
        if any(c.tag in {"p","li","div","section","article", *HEADINGS} for c in n.children):
            continue
        def pieces(node):
            for part in node.parts:
                if isinstance(part, Node):
                    yield from pieces(part)
                else:
                    yield part
        value = " ".join(" ".join(pieces(n)).split())
        if value:
            paragraphs.append(value)
    for i, paragraph in enumerate(paragraphs):
        found = intro.search(paragraph)
        if found:
            listing = paragraph[found.end():].strip()
            if not listing and i + 1 < len(paragraphs):
                listing = paragraphs[i + 1]
            if listing.count("●") >= 2:
                return [s.strip(" .") for s in listing.split("●")]
    return []


def extract_matches(body, url, event, lookup, checked_at, aliases=(), expected_roles=(), from_dated_link=False):
    d = Directory(body)
    year = event["startDate"][:4]
    dated = page_identity(d,url,event,aliases,from_dated_link,body)
    links = directory_links(d,url,event,aliases,dated)
    matches = {}
    def add(value, role):
        if value and role in ROLES:
            cid, rel, display = value
            matches.setdefault((cid,role), dict(companyId=cid,name=display,relationship=rel,role=role,
                                                sourceUrl=url,checkedAt=checked_at[:10]))
    names = trustech_directory(d,event,url)
    if names:
        dated = True
        for name in names:
            add(recognized(name,lookup),"exhibitor")
    # Structured roles only on the identified edition; a schema's separate
    # organization, publisher, venue or other event does not count.
    parsed, _ = collector.extract_events(body)
    for item in parsed:
        if not identifies(item.get("name",""),event,aliases) or str(item.get("startDate",""))[:10] != event["startDate"]:
            continue
        for field,role in (("sponsor","sponsor"),("organizer","organizer"),("exhibitor","exhibitor"),("speaker","speaker")):
            items = item.get(field,[])
            for org in items if isinstance(items,list) else [items]:
                if not isinstance(org,dict):
                    continue
                if field == "speaker":
                    org = org.get("worksFor") or org.get("affiliation") or {}
                if isinstance(org,dict) and org.get("@type") in ("Organization","Corporation"):
                    add(recognized(str(org.get("name","")),lookup),role)
    if not dated:
        return list(matches.values()), links, False
    headers = [n.short() for n in d.nodes if n.tag in ("title","h1")]
    page_roles = roles_in(" ".join(headers))
    # A directory verification requires an edition, a role-bearing heading and
    # a named entry, or a role-bearing section surrounding the named entry.
    if expected_roles and page_roles.isdisjoint(expected_roles):
        page_roles.clear()
    websites = exhibitor_websites(body)
    outlines = defaultdict(dict)
    def scope_of(node):
        return next((a for a in node.ancestors() if a.tag in {"main","nav","aside","header","footer"}),d.root)
    for n in d.nodes:
        # Some pages put a previous-edition heading in its own layout section,
        # immediately before a separate carousel. Preserve the heading outline
        # across those sibling containers without carrying it into another main.
        if n.tag in HEADINGS:
            outline = outlines[scope_of(n)]
            level = int(n.tag[1])
            for deeper in [key for key in outline if key >= level]:
                del outline[deeper]
            if is_old(n.short(),year):
                outline[level] = True
        attrs = str(n.attrs.get("class") or "")
        logo = edition_logo(n,event,aliases)
        if logo:
            if (not outlines.get(scope_of(n)) and not any(a.tag in ("nav","aside","header") for a in n.ancestors())
                    and not section_info(n,year)[1]):
                target = str(n.parent.attrs.get("href") or "")
                host = websites.get(target[1:]) if target.startswith("#") else None
                add(recognized(logo[0],lookup,host),logo[1])
            continue
        host = None
        if n.tag=="button" and "id4a-name-trigger" in attrs.split():
            candidates = [n.short()]
            host = websites.get("id4a-ex-" + str(n.attrs.get("data-id4a-open") or ""))
        elif n.tag == "img":
            mark = organizer_logo(n,url,lookup)
            if mark and not outlines.get(scope_of(n)) and not section_info(n,year)[1]:
                for role in roster_card_roles(n):
                    add(mark,role)
                continue
            candidates = [str(n.attrs.get("alt") or "")]
        elif n.tag in HEADINGS | {"a","td","li","strong","p","div","span"} and (len(n.children) <= 2):
            candidates = [n.short()]
        else:
            continue
        for label in candidates:
            value = recognized(label,lookup,host)
            if not value or outlines.get(scope_of(n)):
                continue
            if any(a.tag in ("nav","aside","header") or a.attrs.get("role")=="navigation" for a in n.ancestors()):
                continue
            context, blocked = section_info(n,year,page_roles)
            if blocked:
                continue
            explicit = roster_card_roles(n)
            roles = explicit or context or page_roles
            if not roles or (len(roles) != 1 and not explicit):
                continue
            role = next(iter(roles))
            if role=="speaker" and not (DIRECT_NAME.search(attrs) or n.tag in ("p","span","div","td")):
                continue
            # A plain link in body prose is a mention, not a directory entry.
            if not (n.tag in HEADINGS | {"img","li","td"} or DIRECT_NAME.search(attrs)
                    or re.search(r"\bsff\d{2}-exl__n\b",attrs) or "id4a-name-trigger" in attrs
                    or any(re.search(r"card|profile|exhibitor|sponsor|speaker|listing|roster|company", str(a.attrs.get("class") or ""),re.I) for a in n.ancestors() if a.tag not in ("body","html"))
                    or (n.tag in ("a","strong") and n.parent and n.parent.tag in ("li","td"))):
                continue
            for verified_role in roles:
                add(value,verified_role)
    return list(matches.values()),links,True


def published_exhibitor_rows(names, lookup, url, stamp):
    matches = {}
    for name in names:
        found = recognized(name, lookup)
        if found:
            cid, relationship, display = found
            matches[cid] = dict(companyId=cid, name=display, relationship=relationship,
                                role="exhibitor", sourceUrl=url, checkedAt=stamp[:10])
    return list(matches.values())


def ofc_directory_names(text, year):
    if not re.search(r"^OFC\s+" + re.escape(year) + r"\s*\n\s*Exhibitor Listing as of", text.lstrip()):
        return []
    if not re.search(r"(?m)^\s*Name\s{2,}Booth\s*$", text):
        return []
    # Only ordinary directory rows carrying a booth number, never incidental text.
    return [m[1].strip() for m in re.finditer(
        r"(?m)^([^\n\r]+?) {2,}[A-Z]-\d+[A-Z]?(?:,\s*[A-Z]-\d+[A-Z]?)*\s*$", text)]


def ofc_directory_text(content):
    import subprocess
    import shutil
    if not content.startswith(b"%PDF-"):
        return ""
    if shutil.which("pdftotext"):
        try:
            proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=content,
                                  capture_output=True, timeout=25, check=True)
            return proc.stdout.decode("utf-8", errors="replace")
        except (subprocess.SubprocessError, OSError):
            return ""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return ""
    try:
        from io import BytesIO
        pdf = PdfReader(BytesIO(content))
        return "\n".join(p.extract_text(extraction_mode="layout") for p in pdf.pages[:100])
    except (PdfReadError, ValueError, OSError):
        return ""


def mexico_directory_names(page, fetcher, delay, rules):
    markup = page["body"]
    if not re.search(r"<title>[^<]*LOUPE Mexico 2027 Exhibitor List", markup, re.I):
        return []
    asset = re.search(r'<link\b[^>]*\brel="modulepreload"[^>]*\bhref="(/assets/routes-[\w-]+\.js)"', markup)
    if not asset:
        return []
    url = urljoin(page["url"], asset[1])
    if rules and not rules.can_fetch(collector.USER_AGENT, url):
        return []
    bundle = fetcher.get(url, delay=delay, robots=rules)
    if bundle["status"] != 200:
        return []
    data = re.search(r'JSON\.parse\(`(\[\{"name":.*?\])`\)', bundle["body"], re.S)
    if not data:
        return []
    rows = json.loads(data[1])
    total = re.search(r'Showing\s+1[–-]\d+\s+of\s+(\d+)\s+exhibitors', markup)
    if (not total or len(rows) != int(total[1]) or not all(isinstance(row, dict) and
            isinstance(row.get("name"), str) and "stand" in row and "country" in row for row in rows)):
        return []
    return [r["name"] for r in rows if r["stand"]]


def scan_event_origin(jobs, lookup, stamp, timeout):
    """Fetch each URL once per origin. Process listed editions separately on shared calendars."""
    fetcher = collector.Fetcher(timeout=timeout)
    first = jobs[0][1][0]
    try:
        _, delay, note, rules = collector.robots_permission(fetcher, first)
        unavailable = "unavailable" in note or delay > 60
    except (OSError, ValueError, http.client.HTTPException):
        delay, rules, unavailable = 2, None, True
    cache = {}
    outcomes = []
    for event, seed, follow in jobs:
        aliases = seed[2]
        queue = [(seed[0], seed[1], False)]
        visited = set()
        pagination_budget = MAX_PAGINATION_PAGES
        while queue:
            url, roles, inherited = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            result = {"eventId": event["id"], "url": url, "state": "unavailable", "matches": []}
            outcomes.append(result)
            if unavailable or (rules and not rules.can_fetch(collector.USER_AGENT,url)):
                continue
            try:
                if url not in cache:
                    cache[url] = fetcher.get(url, delay=delay, robots=rules)
                page = cache[url]
                if page["status"] in (404,410):
                    result["state"] = "removed"
                    continue
                if page["status"] == 200 and "exhibitor" in roles:
                    ofc = (event["id"] == "nov18-ofc-2027" and url == "https://ofc27.mapyourshow.com/8_0/exhibitor/exhibitor-list.cfm?export=pdf")
                    mexico = (event["id"] == "nov13-loupe-mexico" and url == "https://exhibitor-list.loupe-mexico.com/")
                    if ofc or mexico:
                        names = (ofc_directory_names(ofc_directory_text(page.get("pdf", b"")), event["startDate"][:4])
                                 if ofc else mexico_directory_names(page, fetcher, delay, rules))
                        if names:
                            result.update(state="checked", matches=published_exhibitor_rows(names, lookup, url, stamp))
                        continue
                if page["status"] != 200 or "html" not in page["headers"].get("content-type","").lower():
                    continue
                matches, links, dated = extract_matches(page["body"],page["url"],event,lookup,stamp,aliases,roles,inherited)
                state = "checked" if dated or matches else "edition_unverified"
                if state=="edition_unverified":
                    # Generic error/consent/JS shells cannot invalidate an existing citation.
                    headings = " ".join(n.short() for n in Directory(page["body"]).nodes if n.tag in ("title","h1"))
                    years = set(YEAR.findall(headings))
                    if identifies(headings,event,aliases) and years and event["startDate"][:4] not in years:
                        state = "removed"
                result.update(state=state,matches=matches)
                if dated and pagination_budget and (roles or matches):
                    more = pagination_links(Directory(page["body"]),page["url"])
                    remaining = [u for u in more if u not in visited and u not in {t[0] for t in queue}]
                    if remaining:
                        picked = collector.rotate_links(remaining,pagination_budget,stamp)[:pagination_budget]
                        pagination_budget -= len(picked)
                        queue.extend((u,roles,False) for u in picked)
                if follow and url == seed[0]:
                    # Add known same-origin directories even if the homepage itself carries no year.
                    # Such a directory always needs its own independently identified edition.
                    for link, child_roles, linked_to_year in links[:MAX_DIRECTORY_LINKS]:
                        queue.append((link,child_roles,linked_to_year))
            except (ValueError, OSError, http.client.HTTPException, RecursionError, UnicodeError):
                pass
    return outcomes


def read_catalog(root, output, stamp):
    """Include editions accepted by this crawl before checking participation."""
    import refresh
    tree = refresh.source_tree(root)
    imported = json.loads(tree["data/events.json"])
    catalog = json.loads(tree["data/verified-events.json"])["events"] + imported["events"]
    until = date.fromisoformat(imported["meta"]["windowEnd"])
    reference = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    status = output / "collection-status.json"
    if status.is_file():
        run = json.loads(status.read_bytes())
        if run["lastRunAt"] != stamp:
            raise ValueError("Participation and event collection timestamps differ")
        config = json.loads((root / "sources.json").read_bytes())
        candidates = json.loads((output / "review-queue.json").read_bytes())["candidates"]
        catalog, _ = refresh.reconcile(catalog, config, run, candidates, reference, until)
    return [e for e in catalog if refresh.in_window(e, reference, until)]


def scan(root, output, stamp, timeout=8, workers=8):
    config = root / "company-watchlist.json"
    evidence_file = output / "company-evidence.json"
    if not config.is_file() or not (data := json.loads(config.read_bytes())).get("companies"):
        return None  # Empty or absent input means the feature has not been configured.
    lookup = company_index(data)
    recipe = root / "company-sources.json"
    hints = json.loads(recipe.read_bytes())["sources"] if recipe.is_file() else []
    by_event = defaultdict(list)
    for hint in hints:
        if hint.get("role") not in ROLES:
            raise ValueError("Unknown participation role")
        by_event[hint["eventId"]].append(hint)
    groups = defaultdict(list)
    for event in read_catalog(root, output, stamp):
        aliases = sorted({s for h in by_event[event["id"]] for s in h.get("editionAliases", [])})
        start = checked_url(event["sourceUrl"])
        seeds = {start: set()}
        for h in by_event[event["id"]]:
            seeds.setdefault(checked_url(h["url"]),set()).add(h["role"])
        for old in event.get("companyMatches",[]):
            # Revisit previously discovered directories. Never expand the list of authorized origins.
            u=checked_url(old["sourceUrl"])
            if collector.origin(u)==collector.origin(start):
                seeds.setdefault(u,set())
        for u,roles in seeds.items():
            groups[collector.origin(u)].append((event,(u,sorted(roles),aliases),u==start))
    outcomes = []
    with ThreadPoolExecutor(max_workers=max(1,min(workers,12))) as pool:
        for pages in pool.map(lambda jobs: scan_event_origin(jobs,lookup,stamp,timeout),groups.values()):
            outcomes.extend(pages)
    record = {"checkedAt": stamp, "rosterDigest": digest(json.dumps(data,sort_keys=True)),
              "pages": sorted(outcomes,key=lambda p:(p["eventId"],p["url"]))}
    collector.atomic_json(evidence_file,record)
    return record


def apply_snapshot(events, data, evidence, stamp):
    if not data or not data.get("companies") or not evidence:
        return
    index = company_index(data)
    relations = dict(index.values())
    if evidence["checkedAt"]!=stamp or evidence["rosterDigest"]!=digest(json.dumps(data,sort_keys=True)):
        raise ValueError("Company evidence does not match this run's roster or time")
    pages = defaultdict(list)
    for page in evidence["pages"]:
        pages[page["eventId"]].append(page)
    for e in events:
        visited = pages[e["id"]]
        if not visited:
            continue
        success = {p["url"] for p in visited if p["state"] in ("checked","removed")}
        inaccessible = {p["url"] for p in visited if p["state"] in ("unavailable","edition_unverified")} - success
        valid = []
        for old in e.get("companyMatches",[]):
            if old["sourceUrl"] in inaccessible and old["companyId"] in relations:
                valid.append(dict(old,relationship=relations[old["companyId"]]))
        for p in visited:
            if p["state"] != "checked":
                continue
            for m in p["matches"]:
                if (m["companyId"] in relations and m["role"] in ROLES and m["checkedAt"]==stamp[:10]
                    and collector.origin(checked_url(m["sourceUrl"]))==collector.origin(checked_url(p["url"]))):
                    valid.append(dict(m,relationship=relations[m["companyId"]]))
        # Multiple official URLs can support a single role; prefer a current,
        # specific citation and choose deterministically across repeated runs.
        valid.sort(key=lambda m:(m["checkedAt"],len(m["sourceUrl"]),m["sourceUrl"],m["name"]))
        selected = {(m["companyId"],m["role"]):m for m in valid}
        e["companyMatches"] = [selected[k] for k in sorted(selected)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-roster", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Compiled roster file, or sample run directory")
    parser.add_argument("--stamp", help="UTC timestamp of the collection run")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.compile_roster:
        compiled = compile_roster(json.loads(args.compile_roster.read_bytes()))
        collector.atomic_json(args.output,compiled)
        print("Compiled", len(compiled["companies"]), "companies")
    else:
        result = scan(Path(__file__).resolve().parent,args.output,args.stamp or collector.timestamp(),workers=args.workers)
        print("Company evidence pages checked:", len(result["pages"]) if result else 0)


if __name__ == "__main__":
    main()
