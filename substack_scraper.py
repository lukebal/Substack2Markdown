import argparse
import json
import os
import random
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep

import html2text
import markdown
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from tqdm import tqdm

from selenium import webdriver
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.chrome.service import Service
from urllib.parse import urlparse


USE_PREMIUM: bool = True
BASE_SUBSTACK_URL: str = "https://natesnewsletter.substack.com/"
BASE_MD_DIR: str = "substack_md_files"
BASE_HTML_DIR: str = "substack_html_pages"
HTML_TEMPLATE: str = "author_template.html"
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3
COOKIE_FILE: str = "substack_cookies.json"


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')
    return parts[1] if parts[0] == 'www' else parts[0]


def generate_html_file(author_name: str) -> None:
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    html_output_path = os.path.join(BASE_HTML_DIR, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


def load_cookies_as_dict(cookie_file: str) -> dict:
    """
    Reads Cookie-Editor JSON export and returns a plain dict
    suitable for use as a requests session cookie jar.
    """
    with open(cookie_file, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


class BaseSubstackScraper(ABC):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str, cookie_file: str = COOKIE_FILE):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url
        self.cookie_file: str = cookie_file

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        if not os.path.exists(md_save_dir):
            os.makedirs(md_save_dir)
            print(f"Created md directory {md_save_dir}")
        if not os.path.exists(self.html_save_dir):
            os.makedirs(self.html_save_dir)
            print(f"Created html directory {self.html_save_dir}")

        # Build an authenticated requests session using the cookie file
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
            )
        })
        if os.path.exists(cookie_file):
            self.session.cookies.update(load_cookies_as_dict(cookie_file))
            print("Session cookies loaded for authenticated API requests.")
        else:
            print(f"Warning: cookie file not found at {cookie_file}. API requests will be unauthenticated.")

        self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """
        Fetches all post URLs using Substack's internal JSON API with
        authenticated session cookies and exponential backoff on 429s.
        """
        urls = []
        offset = 0
        limit = 12
        api_base = f"{self.base_substack_url}api/v1/posts"

        print(f"Fetching post list from Substack API (authenticated)...")
        while True:
            try:
                response = self.session.get(
                    api_base,
                    params={"limit": limit, "offset": offset},
                    timeout=15
                )

                if response.status_code == 429:
                    wait = 60
                    print(f"API rate limited at offset {offset} — waiting {wait}s before retry...")
                    sleep(wait)
                    # Retry same offset once
                    response = self.session.get(
                        api_base,
                        params={"limit": limit, "offset": offset},
                        timeout=15
                    )
                    if response.status_code == 429:
                        print("Still rate limited after retry. Stopping pagination — will scrape what we have.")
                        break

                if not response.ok:
                    print(f"API returned {response.status_code} at offset {offset}. Stopping.")
                    break

                posts = response.json()
                if not posts:
                    break

                for post in posts:
                    canonical_url = post.get("canonical_url") or post.get("url")
                    if canonical_url:
                        urls.append(canonical_url)

                print(f"  Fetched {len(urls)} post URLs so far...")

                if len(posts) < limit:
                    break

                offset += limit
                sleep(random.uniform(3, 6))  # Polite authenticated delay between pages

            except Exception as e:
                print(f"Error fetching post list at offset {offset}: {e}")
                break

        print(f"Total posts found: {len(urls)}")
        return urls

    @staticmethod
    def html_to_md(html_content: str) -> str:
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if os.path.exists(filepath):
            print(f"File already exists: {filepath}")
            return
        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        return markdown.markdown(md_content, extensions=['extra'])

    def save_to_html_file(self, filepath: str, content: str) -> None:
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")

        html_dir = os.path.dirname(filepath)
        css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
        css_path = css_path.replace("\\", "/")

        html_content = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Markdown Content</title>
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">
                {content}
                </main>
            </body>
            </html>
        """

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md") -> str:
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")
        if not filetype.startswith("."):
            filetype = f".{filetype}"
        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, like_count: str, content: str) -> str:
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        metadata = f"# {title}\n\n"
        if subtitle:
            metadata += f"## {subtitle}\n\n"
        metadata += f"**{date}**\n\n"
        metadata += f"**Likes:** {like_count}\n\n"
        return metadata + content

    def extract_post_data(self, soup: BeautifulSoup) -> Tuple[str, str, str, str, str]:
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"

        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        date = ""
        date_element = soup.select_one("div.pencraft.pc-reset.color-pub-secondary-text-hGQ02T")
        if date_element and date_element.text.strip():
            date = date_element.text.strip()

        if not date:
            script_tag = soup.find("script", {"type": "application/ld+json"})
            if script_tag and script_tag.string:
                try:
                    metadata = json.loads(script_tag.string)
                    if "datePublished" in metadata:
                        date_str = metadata["datePublished"]
                        date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        date = date_obj.strftime("%b %d, %Y")
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass

        if not date:
            date = "Date not found"

        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )

        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)

        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)
        return title, subtitle, like_count, date, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        data_dir = os.path.join(JSON_DATA_DIR)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        json_path = os.path.join(data_dir, f'{self.writer_name}.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            essays_data = existing_data + [data for data in essays_data if data not in existing_data]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(essays_data, f, ensure_ascii=False, indent=4)

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        for url in tqdm(self.post_urls, total=total):
            try:
                md_filename = self.get_filename_from_url(url, filetype=".md")
                html_filename = self.get_filename_from_url(url, filetype=".html")
                md_filepath = os.path.join(self.md_save_dir, md_filename)
                html_filepath = os.path.join(self.html_save_dir, html_filename)

                if not os.path.exists(md_filepath):
                    soup = self.get_url_soup(url)
                    if soup is None:
                        total += 1
                        continue
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)
                    self.save_to_file(md_filepath, md)

                    html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    essays_data.append({
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "date": date,
                        "file_link": md_filepath,
                        "html_link": html_filepath
                    })
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str, cookie_file: str = COOKIE_FILE):
        super().__init__(base_substack_url, md_save_dir, html_save_dir, cookie_file)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            if soup.find("h2", class_="paywall-title"):
                print(f"Skipping premium article: {url}")
                return None
            return soup
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        headless: bool = False,
        edge_path: str = '',
        edge_driver_path: str = '',
        user_agent: str = '',
        cookie_file: str = COOKIE_FILE
    ) -> None:
        options = EdgeOptions()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        if headless:
            options.add_argument("--headless=new")
        if edge_path:
            options.binary_location = edge_path

        _user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
        )
        options.add_argument(f"user-agent={_user_agent}")

        os.environ.setdefault("SE_DRIVER_MIRROR_URL", "https://msedgedriver.microsoft.com")

        self.driver = None

        if edge_driver_path and os.path.exists(edge_driver_path):
            service = Service(executable_path=edge_driver_path)
            self.driver = webdriver.Edge(service=service, options=options)
        else:
            try:
                service = Service(EdgeChromiumDriverManager().install())
                self.driver = webdriver.Edge(service=service, options=options)
            except Exception:
                print("webdriver_manager could not download msedgedriver. Falling back to Selenium Manager.")
                try:
                    self.driver = webdriver.Edge(options=options)
                except SessionNotCreatedException as se:
                    raise RuntimeError(
                        "Selenium Manager fallback failed due to driver/browser mismatch.\n"
                        "Fix by: (a) removing stale msedgedriver from PATH, or "
                        "(b) passing --edge-driver-path to a manually downloaded matching driver."
                    ) from se

        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """
            }
        )

        self._inject_selenium_cookies(base_substack_url, cookie_file)

        # Parent init builds the authenticated requests.Session and fetches post URLs
        super().__init__(base_substack_url, md_save_dir, html_save_dir, cookie_file)

    def _inject_selenium_cookies(self, base_substack_url: str, cookie_file: str) -> None:
        """Injects cookies into the Selenium browser session."""
        if not os.path.exists(cookie_file):
            raise FileNotFoundError(f"Cookie file not found: {cookie_file}")

        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        for domain_url in ["https://substack.com", base_substack_url]:
            self.driver.get(domain_url)
            sleep(3)
            for cookie in cookies:
                c = cookie.copy()
                c.pop("sameSite", None)
                c.pop("hostOnly", None)
                c.pop("storeId", None)
                c["domain"] = ".substack.com"
                try:
                    self.driver.add_cookie(c)
                except Exception:
                    pass
            self.driver.refresh()
            sleep(3)

        print("Cookies loaded. Verifying session...")
        page_source = self.driver.page_source
        if 'sign-in' in page_source.lower() and 'sign out' not in page_source.lower():
            raise Exception(
                "Cookie injection may have failed — still seeing sign-in prompts.\n"
                "Re-export your cookies from Edge while actively logged into substack.com."
            )
        print("Session authenticated. Starting scrape...")

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            self.driver.get(url)
            sleep(random.uniform(4, 9))

            page_source = self.driver.page_source

            if "too many requests" in page_source.lower() or "429" in page_source:
                print(f"Rate limited on {url} — waiting 90 seconds before retry...")
                sleep(90)
                self.driver.get(url)
                sleep(random.uniform(5, 10))
                page_source = self.driver.page_source

                if "too many requests" in page_source.lower() or "429" in page_source:
                    print(f"Still rate limited after retry — skipping: {url}")
                    return None

            return BeautifulSoup(self.driver.page_source, "html.parser")
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument("-u", "--url", type=str, help="The base URL of the Substack site to scrape.")
    parser.add_argument("-d", "--directory", type=str, help="The directory to save scraped posts.")
    parser.add_argument(
        "-n", "--number", type=int, default=0,
        help="The number of posts to scrape. If 0 or not provided, all posts will be scraped.",
    )
    parser.add_argument("-p", "--premium", action="store_true", help="Use the Premium Substack Scraper with Selenium.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--edge-path", type=str, default="", help="Path to the Edge browser executable.")
    parser.add_argument("--edge-driver-path", type=str, default="", help="Path to the Edge WebDriver executable.")
    parser.add_argument("--user-agent", type=str, default="", help="Custom user agent for Selenium.")
    parser.add_argument("--html-directory", type=str, help="Directory to save scraped posts as HTML files.")
    parser.add_argument(
        "--cookies", type=str, default=COOKIE_FILE,
        help=f"Path to Cookie-Editor JSON export for authentication (default: {COOKIE_FILE}).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.directory is None:
        args.directory = BASE_MD_DIR
    if args.html_directory is None:
        args.html_directory = BASE_HTML_DIR

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                args.url,
                headless=args.headless,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                edge_path=args.edge_path,
                edge_driver_path=args.edge_driver_path,
                user_agent=args.user_agent,
                cookie_file=args.cookies
            )
        else:
            scraper = SubstackScraper(
                args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                cookie_file=args.cookies
            )
        scraper.scrape_posts(args.number)
    else:
        if USE_PREMIUM:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                edge_path=args.edge_path,
                edge_driver_path=args.edge_driver_path,
                cookie_file=args.cookies
            )
        else:
            scraper = SubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                cookie_file=args.cookies
            )
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()
