# Use a slim Python image
FROM python:3.11-slim

# Install OS dependencies needed by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gstreamer1.0-libav \
    gstreamer1.0-plugins-good \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files first for better layer caching
COPY requirements.txt /app/

# Install Python deps and Playwright browser
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium \
 && python -m playwright install-deps

# Copy the rest of the app
COPY . /app

# Optional: gunicorn for prod web serving (already in requirements.txt below)
# Expose the port Railway uses (your app will read PORT env)
ENV PORT=8080
EXPOSE 8080

# Start with gunicorn; your Flask app is app:app (from app.py)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "60", "app:app"]