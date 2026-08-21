# Gunakan base image Python yang terbaru
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Instal dependencies system untuk audio (Aplay/ffmpeg jika butuh)
RUN apt-get update && apt-get install -y \
    libasound2-dev \
    portaudio19-dev \
    libportaudio2 \
    libportaudiocpp0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements dan instal
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua kode backend
COPY . .

# Expose port (Hugging Face biasanya port 7860)
EXPOSE 7860

# Jalankan aplikasi dengan gunicorn (lebih reliable untuk web server)
CMD ["gunicorn", "-b", "0.0.0.0:7860", "--timeout", "120", "app:app"]
