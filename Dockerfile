FROM python:3.11-slim

# System tools & libraries (ffmpeg with libass, curl, git, certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-liberation curl unzip git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create user with UID 1000 (standard for Hugging Face Spaces & security)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install Deno for appuser
ENV DENO_INSTALL=/home/appuser/.deno
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y
ENV PATH="/home/appuser/.deno/bin:${PATH}"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Prepare data storage directory
RUN mkdir -p /data && chown -R appuser:appuser /app /data /home/appuser/.deno

USER appuser

# Optimize memory for 512MB RAM cloud containers (Render, Heroku, etc.)
# MALLOC_ARENA_MAX=2 stops glibc from allocating dozens of memory arenas on multi-core hosts
ENV MALLOC_ARENA_MAX=2
ENV PYTHONUNBUFFERED=1
ENV RENDER=true
ENV CLIPFORGE_DATA=/data
ENV CLIPFORGE_HOST=0.0.0.0

CMD ["python", "server.py"]
