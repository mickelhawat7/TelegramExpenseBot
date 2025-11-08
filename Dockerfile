FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps from wheels (no system packages needed)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code
COPY . /app

# Where DB & Excel will be stored (Railway volume should mount here)
ENV DATA_DIR=/data

CMD ["python", "bot.py"]
