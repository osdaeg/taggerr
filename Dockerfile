FROM python:3.12-slim

WORKDIR /app

# chromaprint provee fpcalc + ffmpeg para decodificar audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    libchromaprint-tools \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend/ .
COPY frontend/ /app/frontend/

# Create mount points
RUN mkdir -p /music /art /config

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
