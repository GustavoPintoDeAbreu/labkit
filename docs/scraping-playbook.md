# Scraping playbook: what worked on which site

Read this before adding or fixing a source that reads a website. `labkit.scrape`
is the code; this is what the lab learned using it. Dates matter: walls change.

## Rules

- **Never from the home IP.** Every read goes through the Evomi residential proxy
  (`ProxyFetcher` / `BrowserFetcher`). Without credentials a source is
  unavailable, not "try direct".
- **No hosted fetchers.** Jina Reader, cloud browsers and scraping APIs that fetch
  from their own servers bypass the proxy, its byte cap and its receipts. A paid
  scraping API is a deliberate exception, never a silent rung.
- **Budget by bytes.** A browser read costs 2-5 MB through the proxy. Block images,
  fonts and media (`block_resources`), allow-list hosts (`allow_hosts`), and cap
  MB per day per site.
- **Pace like a person.** Seconds between reads (`delay_s`), one session (one exit
  IP) per fetcher, a new session after a block.

## The ladder: cheapest first, climb only when blocked

1. **Plain HTTP** (`ProxyFetcher`, httpx). Most public JSON APIs and server-rendered
   pages.
2. **Headed Chromium on Xvfb** (`BrowserFetcher`, Playwright). Headless is detected
   far more often than headed; run `xvfb-run` in the container.
3. **Patchright** (`engine="patchright"`, `labkit[stealth]`). Patched Chromium for
   Cloudflare.
4. **Camoufox** (Firefox fork, `headless="virtual"`, `geoip=True`). Not in labkit
   yet; it was probed in a throwaway container. A different engine beats a
   different wall.
5. **Capture the site's own data call** (`capture="<url part>"`). A single-page app
   loads its data after the page. Reading that JSON is cheaper and more stable
   than parsing the DOM.

## Check that the page is real

The Frankensurf benchmark (r/webscraping, 2026-10) found that most failures
weren't blocks. They were HTTP 200 pages with a header and footer and no results.

- A search page must have results, and a product page must have a price. A 200 is
  not success. Classify known-bad pages: challenge, homepage redirect, empty shell,
  promo carousel.
- Some sites ignore the search parameter and show their default feed. Check that
  the results mention what was asked.
- Every wall has a different winner. Record which rung won per site, as below.
- Fresh Ubuntu 24.04 forbids Chrome's sandbox by default, so the browser dies at
  launch. Test on a clean machine.

## Per site

| Site | Wall | What works | Learned |
|---|---|---|---|
| KuantoKusta | Akamai | headed Chromium on Xvfb; httpx and headless are refused | 2026-10-07 |
| Idealista | Cloudflare | Patchright headed, retried on a new session (some exits fail); httpx and Playwright are refused | 2026-10 |
| TAP (booking engine) | Angular app | Patchright + `capture="bfm/rest/booking/availability/search"` on the `booking.flytap.com/booking/flights/deeplink` URL, about 5 MB a read | 2026-10-15 |
| easyJet | Akamai, pages and data API | **nothing yet.** httpx 403; Patchright 403 even after a homepage warm-up; Evomi Scraper API (browser mode) ends in Access Denied; Camoufox reaches "Pick Flights", but `POST /funnel/api/query` (the fares) gets 403 | 2026-10-16 |
| AliExpress | browser-rendered product pages | search data in `_init_data_` JSON; product pages skipped | 2026-10-07 |
| OLX Portugal | refuses plain clients | headed browser fetching only the served document (price-scout `OlxBrowser`); the data is `window.__PRERENDERED_STATE__` | 2026-10 |

Still untried on easyJet: a longer warm-up on one session, other exit countries,
a logged-in session, and Shard Browser (closed-source, recommended for Akamai on
r/webscraping).

## Adding a site

1. Probe with the cheapest rung, through the proxy, and save the raw page
   (house-hunt's `scripts/probe.py` is a model).
2. Climb one rung at a time. Note bytes and seconds per read.
3. Write the parser from a recorded page (fixture), with a test for each known-bad
   page.
4. Add a row to the table above in the same PR.
