import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
DEFAULT_INDEX_PATH = ROOT / "index.qmd"
DEFAULT_OUTPUT_PATH = ROOT / "crossref_results.txt"
DEFAULT_BIB_OUTPUT_PATH = ROOT / "crossref_results.bib"

INDEX_LINK_RE = re.compile(r"\[([^\]]+)\]\(<(reviews/[^>]+\.qmd)>\)")
AUTHORS_RE = re.compile(r"-\s+\*\*Authors\s*&\s*Year:\*\*\s*(.+)")
DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
BIBTEX_KEY_RE = re.compile(r"@\w+\{\s*([^,]+)", re.IGNORECASE)
EXISTING_DOI_RE = re.compile(r'\b(?:doi\s*=\s*[{\"]?|https?://(?:dx\.)?doi\.org/)(10\.\S+?)(?:[}\",\s]|$)', re.IGNORECASE)
TITLE_FIELD_RE = re.compile(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*(?:,|$)", re.IGNORECASE)


def extract_author_year(review_path: Path) -> str:
    if not review_path.exists():
        return ""

    text = review_path.read_text(encoding="utf-8")
    match = AUTHORS_RE.search(text)
    return match.group(1).strip() if match else ""


def load_papers_from_index(index_path: Path) -> list[dict[str, str]]:
    text = index_path.read_text(encoding="utf-8")
    papers = []
    seen_paths = set()

    for title, relative_review_path in INDEX_LINK_RE.findall(text):
        normalized_path = relative_review_path.replace("\\", "/")
        if normalized_path in seen_paths:
            continue

        seen_paths.add(normalized_path)
        review_path = (index_path.parent / normalized_path).resolve()
        papers.append(
            {
                "title": title.strip(),
                "author": extract_author_year(review_path),
                "review_path": normalized_path,
            }
        )

    return papers


def normalize_author_query(author_year: str) -> str:
    author_query = re.sub(r"\([^)]*\d{4}[^)]*\)", "", author_year)
    author_query = re.sub(r"\bet\s+al\.?\b", "", author_query, flags=re.IGNORECASE)
    author_query = author_query.replace("&", " ")
    author_query = re.sub(r"[,:;]", " ", author_query)
    author_query = re.sub(r"\.{2,}", ".", author_query)
    author_query = re.sub(r"\s+\.", ".", author_query)
    author_query = re.sub(r"\s+", " ", author_query).strip()
    author_query = author_query.strip(" .,-")
    return author_query


def build_query(paper: dict[str, str]) -> str:
    author_query = normalize_author_query(paper.get("author", ""))
    title = paper["title"].strip()
    return f'"{title}" {author_query}' if author_query else f'"{title}"'


def safe_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def safe_print(message: str) -> None:
    print(safe_text(message))


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def normalize_for_matching(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def load_existing_references(bib_path: Path) -> dict[str, set[str]]:
    if not bib_path.exists():
        return {"dois": set(), "titles": set(), "keys": set()}

    text = bib_path.read_text(encoding="utf-8", errors="replace")

    dois = {
        re.sub(r"[},\s]+$", "", doi.strip()).lower()
        for doi in EXISTING_DOI_RE.findall(text)
    }
    titles = {
        normalize_for_matching(title)
        for title in TITLE_FIELD_RE.findall(text)
        if normalize_for_matching(title)
    }
    keys = {
        key.strip()
        for key in BIBTEX_KEY_RE.findall(text)
        if key.strip()
    }

    return {"dois": dois, "titles": titles, "keys": keys}


def has_existing_reference(existing_refs: dict[str, set[str]], paper: dict[str, str]) -> bool:
    title = normalize_for_matching(paper["title"])
    return title in existing_refs["titles"]


def fetch_bibtex_for_doi(doi: str) -> str:
    request = Request(
        f"https://doi.org/{doi}",
        headers={
            "Accept": "application/x-bibtex",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace").strip()


def extract_bibtex_key(bibtex: str) -> str:
    match = BIBTEX_KEY_RE.search(bibtex)
    return match.group(1).strip() if match else ""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch first-result metadata from Crossref search for papers listed in index.qmd."
    )
    parser.add_argument(
        "--index-path",
        default=str(DEFAULT_INDEX_PATH),
        help="Path to the Quarto index page to parse (default: index.qmd).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to write the Crossref metadata results table.",
    )
    parser.add_argument(
        "--bib-output",
        default=str(DEFAULT_BIB_OUTPUT_PATH),
        help="Path to write BibTeX entries for matched DOIs.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium in headless mode.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only print the papers discovered from the index page and exit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N discovered papers.",
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Pause for Enter before closing the browser.",
    )
    parser.add_argument(
        "--append-bib",
        action="store_true",
        help="Append only missing BibTeX entries to the BibTeX output file instead of overwriting it.",
    )
    return parser.parse_args()


def extract_first_result(page) -> dict[str, str]:
    results = page.locator("td.item-data")
    if results.count() == 0:
        return {
            "matched_title": "",
            "work_type": "",
            "published": "",
            "journal": "",
            "doi": "",
            "authors": "",
        }

    first = results.first

    matched_title = first.locator("p.lead").first.inner_text().strip() if first.locator("p.lead").count() else ""

    work_type = ""
    published = ""
    journal = ""
    extra = first.locator("p.extra")
    if extra.count():
        extra = extra.first
        bolds = extra.locator("b")
        if bolds.count() >= 1:
            work_type = bolds.nth(0).inner_text().strip()
        if bolds.count() >= 2:
            published = bolds.nth(1).inner_text().strip()

        spans = extra.locator("span")
        if spans.count() >= 2:
            journal_text = spans.nth(1).inner_text().strip()
            journal = re.sub(r"^\s*in\s+", "", journal_text, flags=re.IGNORECASE).strip()

    authors = ""
    authors_block = first.locator("p.expand")
    if authors_block.count():
        authors = authors_block.first.inner_text().strip()
        authors = re.sub(r"^\s*Authors:\s*", "", authors).strip()
        authors = authors.replace(" | ", "; ")

    doi = ""
    doi_links = first.locator("div.item-links a[href*='doi.org']")
    if doi_links.count():
        doi_url = doi_links.first.get_attribute("href") or ""
        doi = DOI_PREFIX_RE.sub("", doi_url).strip()

    return {
        "matched_title": matched_title,
        "work_type": work_type,
        "published": published,
        "journal": journal,
        "doi": doi,
        "authors": authors,
    }


def write_results(output_path: Path, results: list[dict[str, str]]) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        f.write(
            "Query Title | Query Authors | Crossref Title | Type | Published | Journal | DOI | Authors | BibTeX Key\n"
        )
        f.write("---|---|---|---|---|---|---|---|---\n")
        for result in results:
            f.write(
                " | ".join(
                    markdown_escape(result[key])
                    for key in [
                        "query_title",
                        "query_authors",
                        "matched_title",
                        "work_type",
                        "published",
                        "journal",
                        "doi",
                        "authors",
                        "bibtex_key",
                    ]
                )
                + "\n"
            )


def write_bibtex_results(output_path: Path, results: list[dict[str, str]]) -> None:
    bib_entries = [result["bibtex"].strip() for result in results if result.get("bibtex", "").strip()]
    with output_path.open("w", encoding="utf-8") as f:
        if bib_entries:
            f.write("\n\n".join(bib_entries) + "\n")


def append_bibtex_results(output_path: Path, results: list[dict[str, str]]) -> int:
    existing_refs = load_existing_references(output_path)
    bib_entries_to_append = []

    for result in results:
        bibtex = result.get("bibtex", "").strip()
        doi = result.get("doi", "").strip().lower()
        title = normalize_for_matching(result.get("matched_title", "") or result.get("query_title", ""))
        bibtex_key = result.get("bibtex_key", "").strip()

        if not bibtex:
            continue
        if doi and doi in existing_refs["dois"]:
            continue
        if title and title in existing_refs["titles"]:
            continue
        if bibtex_key and bibtex_key in existing_refs["keys"]:
            continue

        bib_entries_to_append.append(bibtex)
        if doi:
            existing_refs["dois"].add(doi)
        if title:
            existing_refs["titles"].add(title)
        if bibtex_key:
            existing_refs["keys"].add(bibtex_key)

    if bib_entries_to_append:
        prefix = "\n\n" if output_path.exists() and output_path.read_text(encoding="utf-8", errors="replace").strip() else ""
        with output_path.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n\n".join(bib_entries_to_append) + "\n")

    return len(bib_entries_to_append)


def run():
    args = parse_args()
    index_path = Path(args.index_path).resolve()
    output_path = Path(args.output).resolve()
    bib_output_path = Path(args.bib_output).resolve()

    papers = load_papers_from_index(index_path)
    if args.limit is not None:
        papers = papers[: args.limit]

    if not papers:
        raise ValueError(f"No review links were found in {index_path}")

    safe_print(f"Loaded {len(papers)} papers from {index_path.name}")

    existing_refs = load_existing_references(bib_output_path) if args.append_bib else {"dois": set(), "titles": set(), "keys": set()}

    if args.append_bib:
        missing_papers = []
        skipped_count = 0
        for paper in papers:
            if has_existing_reference(existing_refs, paper):
                skipped_count += 1
            else:
                missing_papers.append(paper)
        papers = missing_papers
        safe_print(f"Existing reference file checked: {skipped_count} already present, {len(papers)} missing.")

    if args.list_only:
        for i, paper in enumerate(papers, start=1):
            safe_print(f"[{i}] {paper['title']} | {paper.get('author', '')} -> {paper['review_path']}")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        results = []
        safe_print(f"Starting Crossref lookup for {len(papers)} papers...")

        for i, paper in enumerate(papers, start=1):
            query = build_query(paper)
            url = f"https://search.crossref.org/search/works?q={quote_plus(query)}&from_ui=yes"

            safe_print(f"[{i}/{len(papers)}] Searching Crossref for: {paper['title'][:70]}...")

            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle")
                try:
                    page.wait_for_selector("td.item-data", timeout=5000)
                except PlaywrightTimeoutError:
                    pass

                extracted = extract_first_result(page)
                bibtex = ""
                bibtex_key = ""
                if extracted["doi"]:
                    try:
                        bibtex = fetch_bibtex_for_doi(extracted["doi"])
                        bibtex_key = extract_bibtex_key(bibtex)
                    except Exception as bibtex_error:
                        safe_print(f"   BibTeX fetch error: {bibtex_error}")

                extracted.update(
                    {
                        "query_title": paper["title"],
                        "query_authors": normalize_author_query(paper.get("author", "")),
                        "bibtex": bibtex,
                        "bibtex_key": bibtex_key,
                    }
                )
                results.append(extracted)

                matched = extracted["matched_title"] or "No result found"
                doi_text = extracted["doi"] or "No DOI"
                safe_print(f"   Match: {matched}")
                safe_print(f"   DOI: {doi_text}")
                if bibtex_key:
                    safe_print(f"   BibTeX key: {bibtex_key}")

            except Exception as e:
                safe_print(f"   Error: {e}")
                results.append(
                    {
                        "query_title": paper["title"],
                        "query_authors": normalize_author_query(paper.get("author", "")),
                        "matched_title": "",
                        "work_type": "Error",
                        "published": "",
                        "journal": "",
                        "doi": "",
                        "authors": "",
                        "bibtex": "",
                        "bibtex_key": "",
                    }
                )

            time.sleep(1)

        write_results(output_path, results)
        if args.append_bib:
            appended_count = append_bibtex_results(bib_output_path, results)
            safe_print(f"Appended {appended_count} new BibTeX entr{'y' if appended_count == 1 else 'ies'} to {bib_output_path}")
        else:
            write_bibtex_results(bib_output_path, results)
        safe_print(f"\nResults saved to {output_path}")
        safe_print(f"BibTeX saved to {bib_output_path}")

        if args.pause:
            safe_print("Press Enter to close the browser.")
            input()

        browser.close()


if __name__ == "__main__":
    run()