FROM node:20-bookworm-slim

# --- Python 3 for the FlightRadar24 sidecar ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Isolated virtualenv so pip installs don't fight Debian's system-managed
# Python (PEP 668) -- PATH is set so plain `python3`/`python` resolve to
# this venv, which is exactly what server.js's child_process.spawn looks
# for (see PYTHON_COMMANDS in server.js).
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install Node dependencies first (better layer caching on rebuilds)
COPY package*.json ./
RUN npm install --omit=dev

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the app
COPY . .

# Render sets $PORT at runtime and server.js already reads
# process.env.PORT -- this default just matters for local `docker run`.
ENV PORT=3000
EXPOSE 3000

# Where fleet_core.py's cache/debug files get written -- overridden to
# /app/data here since there's no "~/Desktop" on Linux. Mount a Render
# persistent Disk at this path if you want the sector-memory cache to
# survive redeploys (optional -- see the README).
ENV FLEET_DATA_DIR=/app/data

CMD ["node", "server.js"]