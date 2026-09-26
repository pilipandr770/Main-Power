FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLASK_APP=wsgi.py
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x deploy/entrypoint.sh && useradd -m app && mkdir -p /app/instance && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["gunicorn", "-w", "3", "-k", "gthread", "--threads", "4", "--timeout", "120", "-b", "0.0.0.0:8000", "wsgi:app"]
