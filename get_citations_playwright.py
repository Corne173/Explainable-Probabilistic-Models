import argparse
import random
import re
import sys
import time
from urllib.parse import quote_plus
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
DEFAULT_INDEX_PATH = ROOT / "index.qmd"
DEFAULT_OUTPUT_PATH = ROOT / "citation_results.txt"

INDEX_LINK_RE = re.compile(r"\[([^\]]+)\]\(<(reviews/[^>]+\.qmd)>\)")
AUTHORS_RE = re.compile(r"-\s+\*\*Authors\s*&\s*Year:\*\*\s*(.+)")


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


def build_query(paper: dict[str, str]) -> str:
    title = paper["title"]
    author = paper.get("author", "").strip()
    return f'"{title}" {author}' if author else f'"{title}"'


def safe_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def safe_print(message: str) -> None:
    print(safe_text(message))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch Google Scholar citation counts for papers listed in index.qmd."
    )
    parser.add_argument(
        "--index-path",
        default=str(DEFAULT_INDEX_PATH),
        help="Path to the Quarto index page to parse (default: index.qmd).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to write the citation results table.",
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
    return parser.parse_args()


def run():
    args = parse_args()
    index_path = Path(args.index_path).resolve()
    output_path = Path(args.output).resolve()

    papers = load_papers_from_index(index_path)
    if args.limit is not None:
        papers = papers[: args.limit]

    if not papers:
        raise ValueError(f"No review links were found in {index_path}")

    safe_print(f"Loaded {len(papers)} papers from {index_path.name}")

    if args.list_only:
        for i, paper in enumerate(papers, start=1):
            author_text = f" | {paper['author']}" if paper.get("author") else ""
            safe_print(f"[{i}] {paper['title']}{author_text} -> {paper['review_path']}")
        return

    with sync_playwright() as p:
        # Launch browser (non-headless so you can see it)
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        results = []
        safe_print(f"Starting crawl for {len(papers)} papers...")

        for i, paper in enumerate(papers):
            query = build_query(paper)
            url = f"https://scholar.google.com/scholar?q={quote_plus(query)}"

            safe_print(f"[{i+1}/{len(papers)}] Searching for: {paper['title'][:50]}...")
            
            try:
                page.goto(url)
                
                # Check for CAPTCHA manually in the UI
                if "not a robot" in page.content() or "unusual traffic" in page.content():
                    safe_print("!!! CAPTCHA DETECTED !!! Solve it in the browser window to continue.")
                    # Wait for the results to appear after you solve the captcha
                    page.wait_for_selector(".gs_rt, #gs_res_ccl_mid", timeout=0)
                
                # Small sleep to let elements load
                time.sleep(1)

                # Find the "Cited by" link
                citation_text = "0"
                links = page.query_selector_all("a")
                for link in links:
                    text = link.inner_text()
                    if "Cited by" in text:
                        match = re.search(r'Cited by (\d+)', text)
                        if match:
                            citation_text = match.group(1)
                            break
                
                safe_print(f"   Result: {citation_text} citations")
                results.append({"title": paper["title"], "citations": citation_text})

            except Exception as e:
                safe_print(f"   Error: {e}")
                results.append({"title": paper["title"], "citations": "Error"})

            # Random sleep between 3-6 seconds
            time.sleep(random.uniform(3, 6))

        # Output to file
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("Citations | Title\n---|---\n")
            for r in results:
                f.write(f"{r['citations']} | {r['title']}\n")

        safe_print(f"\nResults saved to {output_path}")
        safe_print("Keep the browser open if you want to inspect. Press Enter in terminal to close.")
        input()
        browser.close()

if __name__ == "__main__":
    run()
