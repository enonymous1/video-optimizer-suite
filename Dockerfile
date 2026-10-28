FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app/
RUN pip install --no-cache-dir -r requirements.txt
# Media + state are mounted at runtime; nothing library-specific is baked in.
VOLUME ["/mnt/media", "/app/state"]
CMD ["python", "pipeline.py", "--help"]
