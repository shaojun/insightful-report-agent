FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
RUN sed -i "s|http://deb.debian.org|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    bash ca-certificates ffmpeg fonts-noto-cjk libreoffice-writer pandoc poppler-utils \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home node
RUN pip install --no-cache-dir 'Pillow==12.2.0' 'markdown-it-py==4.0.0' 'beautifulsoup4==4.14.3' 'charset-normalizer==3.4.4'
WORKDIR /workspace
CMD ["sleep", "infinity"]
