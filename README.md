---
title: Google Maps Keyword Scraper
emoji: 🗺️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Google Maps Keyword Scraper

A Streamlit app that searches Google Maps for a keyword in a location and
extracts the fields you pick — name, address, phone, coordinates, rating,
review count, category — with CSV download.

## Running locally

Needs Python 3.9+ and Chrome or Chromium installed. Selenium resolves the
driver itself, so there is nothing else to set up.

```bash
pip install -r requirements.txt
streamlit run scraper_ui.py
```

## Running with Docker

```bash
docker build -t maps-scraper .
docker run --rm -p 7860:7860 maps-scraper
```

Then open http://localhost:7860.

## How the browser is located

`find_browser_binary()` resolves Chrome in this order:

1. `$CHROME_BIN`, if set and present — this is what the Docker image uses.
2. `chromium`, `chromium-browser`, `google-chrome`, `google-chrome-stable` on `PATH`.
3. Known locations: `/usr/bin`, and macOS app bundles.
4. Nothing found, on Linux only: Selenium Manager downloads Chrome for Testing.

Step 4 is a fallback for hosts with no browser. It downloads the browser but
**not** the system libraries Chrome links against, so on a minimal image it
fails with exit code 127. When that happens the app runs `ldd` and names the
missing libraries in the error. The Docker image avoids this entirely by
installing Chromium from Debian.

## A note on hosting

This needs a host where you control the image, because Chromium has to be
installed at build time.

It does **not** work on Streamlit Community Cloud. Installing Chromium there
needs `packages.txt`, and since Debian 11 "bullseye" reached end of LTS on
2026-08-31 its expired security repository makes `apt-get update` exit
non-zero, which fails the build for every app that ships one. Without root
there is no supported way to install the libraries by hand.

Hugging Face Spaces moved the Docker SDK behind a paid plan in July 2026, so
a free personal account can no longer create one. The Space frontmatter above
is kept for anyone on PRO.

The image reads `$PORT`, so it runs unmodified on Cloud Run, Koyeb, Render and
similar, defaulting to 7860 for Spaces.

Separately, Google serves consent walls and CAPTCHAs to datacenter IP ranges,
so cloud deployments often return no results even with a working browser.
Running locally from a residential connection is considerably more reliable.

## Limitations

- Selectors are pinned to Google's obfuscated CSS class names
  (`div.fontHeadlineSmall`, `span.MW4etd`, `span.UY7F9`). Google rotates these
  periodically; when that happens fields come back empty and the selectors in
  `scrape()` need updating.
- Results are deduplicated by name, so two branches of one chain in the same
  search collapse into a single row.
- Detailed Mode opens each result in turn and is much slower than list-only
  extraction.
