FROM python:3.12-slim

# ffmpeg = แปลงไฟล์/รวมภาพเสียง, deno = ตัวรัน JS ที่ yt-dlp ใช้ถอดรหัส YouTube
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN useradd -m app && mkdir -p /data && chown -R app /data /app
USER app

EXPOSE 8080
# โปรเซสเดียว (งานเก็บในหน่วยความจำ) + หลาย thread
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 16 -b 0.0.0.0:${PORT:-8080} --timeout 300 app:app"]
