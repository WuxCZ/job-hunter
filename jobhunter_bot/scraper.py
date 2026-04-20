from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from jobhunter_bot.db import JobListing

DETAIL_URL_RE = re.compile(r"^https://www\.jobs\.cz/rpd/\d+")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "cs,en;q=0.9",
}


@dataclass
class JobDetailSummary:
    title: str
    company: str
    location: str
    snippet: str

    def format_text(self) -> str:
        lines = [
            f"Pozice: {self.title or '—'}",
            f"Firma: {self.company or '—'}",
            f"Lokalita: {self.location or '—'}",
            "",
            "Náplň práce / popis (z inzerátu):",
            self.snippet or "Nepodařilo se načíst text náplně – otevři detail na Jobs.cz.",
        ]
        return "\n".join(lines)


def build_jobs_search_url(locality: str, query: str, radius_km: int) -> str:
    locality_clean = (locality or "brno").strip().lower().replace(" ", "-")
    params: list[tuple[str, str]] = [("locality[radius]", str(max(1, radius_km)))]
    if query.strip():
        params.append(("q[]", query.strip()))
    return f"https://www.jobs.cz/prace/{quote_plus(locality_clean)}/?{urlencode(params)}"


def _split_title_and_company(raw_title: str) -> tuple[str, str]:
    """Rozdělí např. „Pozice – Firma“ nebo „Pozice - Firma (og:title z Jobs.cz)."""
    if not raw_title:
        return "", ""
    t = raw_title.strip()
    parts = re.split(r"\s+[–—]\s+", t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    parts2 = re.split(r"\s+-\s+", t, maxsplit=1)
    if len(parts2) == 2 and len(parts2[1]) > 2:
        return parts2[0].strip(), parts2[1].strip()
    return t, ""


def _walk_json_for_job_fields(obj, company: list[str], location: list[str]) -> None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (t and "Job" in str(t)):
            org = obj.get("hiringOrganization") or obj.get("publisher")
            if isinstance(org, dict) and org.get("name"):
                company.append(str(org["name"]).strip())
            jl = obj.get("jobLocation")
            if isinstance(jl, list) and jl:
                jl = jl[0]
            if isinstance(jl, dict):
                addr = jl.get("address")
                if isinstance(addr, dict):
                    loc = ", ".join(
                        p
                        for p in (
                            addr.get("addressLocality"),
                            addr.get("streetAddress"),
                            addr.get("addressRegion"),
                        )
                        if p
                    )
                    if loc:
                        location.append(loc)
                if jl.get("name") and not location:
                    location.append(str(jl["name"]).strip())
        for v in obj.values():
            _walk_json_for_job_fields(v, company, location)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_job_fields(item, company, location)


def _parse_next_data(html: str) -> tuple[str, str, str]:
    """Vytáhne firmu/lokalitu z Next.js __NEXT_DATA__ (Jobs.cz)."""
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return "", "", ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return "", "", ""
    company: list[str] = []
    location: list[str] = []
    _walk_json_for_job_fields(data, company, location)
    title_guess = ""
    props = data.get("props", {})
    page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
    if isinstance(page_props, dict):
        job = page_props.get("job") or page_props.get("advertisement") or page_props.get("offer")
        if isinstance(job, dict):
            title_guess = (job.get("title") or job.get("name") or "") or title_guess
            emp = job.get("employer") or job.get("company")
            if isinstance(emp, dict) and emp.get("name"):
                company.insert(0, str(emp["name"]).strip())
            loc = job.get("locality") or job.get("location")
            if isinstance(loc, str) and loc.strip():
                location.insert(0, loc.strip())
            elif isinstance(loc, dict):
                if loc.get("label"):
                    location.insert(0, str(loc["label"]).strip())
    return (
        title_guess,
        company[0] if company else "",
        location[0] if location else "",
    )


def _strip_html_to_text(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    t = raw.strip()
    if "<" in t and ">" in t:
        t = BeautifulSoup(t, "html.parser").get_text(" ", strip=True)
    return " ".join(t.split())


def _extract_job_body_from_next_json(obj, depth: int = 0) -> str:
    """Nejdelší věrohodný text z __NEXT_DATA__ (náplň / popis pozice), ne SEO og."""
    if depth > 35:
        return ""
    best = ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            prefer = any(
                x in lk
                for x in (
                    "description",
                    "jobdescription",
                    "htmlbody",
                    "htmlcontent",
                    "advertisement",
                    "positiontext",
                    "body",
                    "content",
                    "introduction",
                    "tasks",
                    "requirements",
                    "responsibilit",
                    "candidate",
                )
            )
            skip = any(x in lk for x in ("url", "logo", "image", "og", "meta", "email", "phone", "salary"))
            if isinstance(v, str) and len(v) > 120 and prefer and not skip:
                cleaned = _strip_html_to_text(html_module.unescape(v))
                if len(cleaned) > len(best) and len(cleaned) > 150:
                    best = cleaned
            sub = _extract_job_body_from_next_json(v, depth + 1)
            if len(sub) > len(best):
                best = sub
    elif isinstance(obj, list):
        for item in obj:
            sub = _extract_job_body_from_next_json(item, depth + 1)
            if len(sub) > len(best):
                best = sub
    return best


def _job_body_from_next_data_html(raw_html: str) -> str:
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        raw_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""
    return _extract_job_body_from_next_json(data)


def _job_body_from_dom(soup: BeautifulSoup) -> str:
    """Viditelná náplň z DOM (ne meta description)."""
    selectors = (
        "[data-testid='job-description']",
        "[data-test='job-description']",
        "[data-test='section-description']",
        "[data-test='advertisement-description']",
        "article [class*='description']",
        "main article",
        "[class*='JobDetail'] [class*='RichText']",
        "[class*='rich-text']",
    )
    candidates: list[str] = []
    for sel in selectors:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if len(t) > 200:
                candidates.append(t)
    if not candidates:
        return ""
    return max(candidates, key=len)


def fetch_job_detail(url: str, timeout_seconds: int) -> JobDetailSummary:
    """Načte krátký souhrn z detailu inzerátu (HTML)."""
    try:
        response = requests.get(url, timeout=timeout_seconds, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        raw_html = response.text
    except Exception:
        return JobDetailSummary(title="", company="", location="", snippet="")

    soup = BeautifulSoup(raw_html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    position_only, company_from_title = _split_title_and_company(title)

    company = ""
    location = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in ("JobPosting", "WebPage"):
                hiring = item.get("hiringOrganization") or item.get("publisher")
                if isinstance(hiring, dict):
                    company = hiring.get("name") or company
                job_loc = item.get("jobLocation")
                if isinstance(job_loc, list) and job_loc:
                    job_loc = job_loc[0]
                if isinstance(job_loc, dict):
                    addr = job_loc.get("address")
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("addressLocality"),
                            addr.get("addressRegion"),
                            addr.get("streetAddress"),
                        ]
                        location = ", ".join(p for p in parts if p) or location
                    if job_loc.get("name") and not location:
                        location = str(job_loc["name"]).strip()

    nt, nc, nl = _parse_next_data(raw_html)
    if nc:
        company = nc
    if nl:
        location = nl
    if nt and not position_only:
        position_only = nt

    if not company and company_from_title:
        company = company_from_title

    if not position_only and title:
        position_only = title

    snippet = ""
    next_body = _job_body_from_next_data_html(raw_html)
    dom_body = _job_body_from_dom(soup)
    if len(next_body) >= len(dom_body) and len(next_body) > 200:
        snippet = next_body
    elif len(dom_body) > 200:
        snippet = dom_body
    else:
        snippet = next_body or dom_body

    if not snippet or len(snippet) < 200:
        ogd = soup.find("meta", property="og:description")
        if ogd and ogd.get("content"):
            snippet = ogd["content"].strip()
    if not snippet:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            snippet = md["content"].strip()
    if not snippet:
        main = soup.select_one(
            "main article, [data-test='job-description'], [data-testid='job-description'], .description"
        )
        if main:
            snippet = main.get_text(" ", strip=True)[:1200]

    for sel in (
        "[data-test='company-name']",
        "[data-testid='company-name']",
        "a[href*='/zamestnavatel/']",
    ):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True) and not company:
            company = " ".join(el.get_text(" ", strip=True).split())
            break

    for sel in (
        "[data-test='locality']",
        "[data-test='job-locality']",
        "[data-testid='locality']",
    ):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True) and not location:
            location = " ".join(el.get_text(" ", strip=True).split())
            break

    if len(snippet) > 12000:
        snippet = snippet[:11997] + "…"

    return JobDetailSummary(
        title=position_only or title,
        company=company,
        location=location,
        snippet=snippet,
    )


def _search_url_for_page(base_url: str, page: int) -> str:
    """Jobs.cz SERP používá query param `page` (2, 3, …). Strana 1 = bez `page`."""
    p = urlparse(base_url)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        pairs.append(("page", str(page)))
    return urlunparse(p._replace(query=urlencode(pairs)))


def _parse_listings_from_search_html(html: str) -> list[JobListing]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    listings: list[JobListing] = []

    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue

        url = urljoin("https://www.jobs.cz", href)
        if not DETAIL_URL_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)

        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title:
            continue

        card = anchor.find_parent(["article", "section", "div"])
        company = ""
        if card is not None:
            company_el = card.select_one("[data-test='company-name'], .company, .css-1f3vzok")
            if company_el is not None:
                company = " ".join(company_el.get_text(" ", strip=True).split())

        listings.append(JobListing(title=title, company=company, url=url))

    return listings


def scrape_jobs(
    search_url: str,
    timeout_seconds: int,
    *,
    max_listings: int | None = None,
) -> list[JobListing]:
    """
    Načte inzeráty z výpisu Jobs.cz.
    Pokud je `max_listings` zadané, stáhne více stránek (`page=2`, …), dokud
    nenabere dostatek unikátních odkazů nebo stránka nepřinese 0 nových.
    """
    if max_listings is None:
        response = requests.get(search_url, timeout=timeout_seconds, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        return _parse_listings_from_search_html(response.text)

    out: list[JobListing] = []
    seen: set[str] = set()
    page_num = 1
    while len(out) < max_listings and page_num <= 80:
        url = _search_url_for_page(search_url, page_num)
        response = requests.get(url, timeout=timeout_seconds, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        batch = _parse_listings_from_search_html(response.text)
        if not batch:
            break
        added = 0
        for listing in batch:
            if listing.url in seen:
                continue
            seen.add(listing.url)
            out.append(listing)
            added += 1
            if len(out) >= max_listings:
                return out
        if added == 0:
            break
        page_num += 1
    return out
