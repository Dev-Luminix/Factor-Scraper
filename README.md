# Factor Scraper

This repository crawls one real-estate investor website when its GitHub Actions
workflow is dispatched. It cleans and prioritizes likely project/location text,
then signs and posts the result to the monday code backend. It does **not** call
monday or OpenAI directly.

## Setup

1. Keep this repository private if the URLs and crawl logs should not be public.
2. In GitHub, open **Settings → Secrets and variables → Actions** and create:
   - `CALLBACK_URL`: `https://<your-monday-code-server>/investor-cities/crawl-result`
   - `CALLBACK_SECRET`: the exact same high-entropy value configured as
     `CALLBACK_SECRET` in the monday code backend.
3. Create a fine-grained personal access token for the monday backend:
   - Resource owner: the account that owns `Dev-Luminix/Factor-Scraper`.
   - Repository access: **Only select repositories** → `Factor-Scraper`.
   - Repository permissions: **Actions: Read and write**.
   - Set the token as `GITHUB_TOKEN` in the monday code backend. Also set
     `GITHUB_OWNER=Dev-Luminix`, `GITHUB_REPO=Factor-Scraper`,
     `GITHUB_WORKFLOW_FILE=crawl.yml`, and `GITHUB_REF=main`.

The backend dispatches the workflow; do not put the GitHub token in this
repository or Actions secrets.

## Test from GitHub

Open **Actions → Crawl investor website → Run workflow**, supply a URL, item ID,
and board ID, then run it. Use an item and board from the development board: a
successful callback writes to that item through the monday backend.

## Local dry run

Python 3.11 is required. From the repository root:

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows PowerShell
# source .venv/bin/activate             # macOS/Linux
python -m pip install -r requirements.txt
python -m patchright install chromium
python crawl.py --url https://example.com --item-id 1 --board-id 1 --dry-run
```

`--dry-run` prints the payload and never sends a callback. A normal local run
requires `CALLBACK_URL` and `CALLBACK_SECRET` environment variables.

## Callback contract

The crawler sends one UTF-8 JSON body to `CALLBACK_URL` with
`Content-Type: application/json` and:

```
X-Signature: sha256=<hex HMAC-SHA256 of the exact body bytes>
```

The HMAC key is `CALLBACK_SECRET`. The body is serialized exactly once before it
is signed and sent:

```json
{
  "item_id": "string",
  "board_id": "string",
  "url": "string",
  "status": "success",
  "error": null,
  "content_hash": "sha256 hex",
  "pages": [{ "url": "string", "title": "string", "text": "string" }],
  "crawled_at": "2026-01-01T00:00:00Z"
}
```

If a crawl cannot run, the payload has `status: "error"`, an explanatory
`error`, and an empty `pages` array. Callback delivery retries up to three times
after the initial attempt for network errors and HTTP 5xx responses. A final
workflow failure step reports an error callback if no result was delivered.

## Crawl behaviour

- URLs are normalized and redirects establish the final same-site domain;
  `www` and non-`www` variants are treated as one site.
- The crawler respects `robots.txt`, uses a clear user agent, and waits between
  requests. It never intentionally follows links off the final site.
- It uses sitemaps first, then a same-domain depth-two browser crawl when no
  useful sitemap is found. The selected crawl is capped at 30 pages.
- Navigation, footers, consent UI, scripts, and repeated site-wide text are
  removed. Project/location/address/unit material is prioritized and the final
  text is capped at 60,000 characters.
