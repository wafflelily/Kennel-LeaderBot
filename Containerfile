FROM python:3.12.12-slim-trixie

# Fonts for the invite map images; the slim image has none, and Pillow's
# built-in fallback has no bold. (The Krillion table bundles its own font.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot
COPY . /bot

RUN python -m pip install -r requirements.txt

ENTRYPOINT [ "python", "bot.py" ]