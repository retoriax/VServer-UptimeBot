# Use official Python image as base
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements if present, else install directly
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt || true

# Copy the rest of the code (settings.txt is now the only persistent config file)
COPY uptimebot.py ./

# Expose no ports (Telegram bot is outbound only)

# Set environment variables (override with .env at runtime)
ENV PYTHONUNBUFFERED=1

# Default command
CMD ["python", "uptimebot.py"]
