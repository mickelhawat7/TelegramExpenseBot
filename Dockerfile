# --- Base Python image
FROM python:3.11-slim

# Avoid Python buffering logs
ENV PYTHONUNBUFFERED=1

# Work in /app
WORKDIR /app

# System packages (optional but helpful for pandas/matplotlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libatlas-base-dev libfreetype6-dev libpng-dev \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your bot code
COPY . /app

# Where DB & Excel will be stored (Railway volume should mount here)
ENV DATA_DIR=/data

# Start the bot
CMD ["python", "bot.py"]
