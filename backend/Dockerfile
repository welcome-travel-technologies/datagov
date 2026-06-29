# Use official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY app/requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN pip install gunicorn

# Copy project
COPY app/ /app/

# Copy and set entrypoint (collectstatic runs at startup, after volumes are mounted)
COPY entrypoint.sh /entrypoint.sh
# Strip any CR characters (Windows checkouts can introduce CRLF) so the shebang works
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Expose port
EXPOSE 8000

# Run gunicorn with uvicorn workers for ASGI support
# Using a 300s timeout to allow long LLM requests in the chatbot to finish
CMD ["/entrypoint.sh"]
