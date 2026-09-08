# Chromium comes from Debian here, so Selenium Manager never has to download a
# browser at runtime and there are no missing shared libraries to chase.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Hugging Face Spaces runs the container as UID 1000; matching it keeps the
# app directory and Streamlit's config dir writable.
RUN useradd --create-home --uid 1000 appuser
USER appuser
ENV HOME=/home/appuser \
    PATH=/home/appuser/.local/bin:$PATH
WORKDIR /home/appuser/app

COPY --chown=appuser:appuser requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

# Hosts disagree on the port they expect: Spaces wants 7860, Cloud Run and
# Koyeb inject $PORT, Render uses 10000. Shell-form CMD lets it be resolved at
# run time instead of baked in.
ENV PORT=7860
EXPOSE 7860

CMD streamlit run scraper_ui.py \
      --server.port=${PORT:-7860} \
      --server.address=0.0.0.0 \
      --server.headless=true \
      --browser.gatherUsageStats=false
