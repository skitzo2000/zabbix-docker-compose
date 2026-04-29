FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "--timeout", "60", "app:app"]
