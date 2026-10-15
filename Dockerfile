# Docs agent + MCP server. Browser + ffmpeg come from the official Playwright image (match playwright in requirements).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
# ffmpeg with H.264 for `export --mp4` (Playwright's bundled ffmpeg only writes VP8/webm)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# Keep playwright at the image's version — a newer one looks for a browser build the image doesn't have.
RUN echo "playwright==1.63.0" > /tmp/pin.txt && pip install --no-cache-dir -r requirements.txt -c /tmp/pin.txt

COPY agent.py mcp_server.py exporter.py ./

# projects/<name>/ (guides.yaml, .env, out/) is mounted from the host: -v "$PWD/projects:/app/projects"
# Keys + logins come from --env-file; nothing secret is baked into the image.
ENV HEADLESS=1 PYTHONUNBUFFERED=1 PROJECTS_DIR=/app/projects
VOLUME /app/projects

# Default: MCP server on stdio (Claude Desktop). Override for the CLI, e.g. `python agent.py plan --project x`.
CMD ["python", "mcp_server.py"]
