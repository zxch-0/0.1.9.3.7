# Optional. Render does not need this file (it uses "runtime: python"), but it makes
# the project runnable on any Docker host:  docker build -t formbot . && docker run --env-file .env -p 10000:10000 formbot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The free Render tier has no persistent disk; /tmp is the only writable place we rely on.
ENV DATA_DIR=/tmp/formbot-data
RUN mkdir -p /tmp/formbot-data

COPY main.py ./
COPY formbot ./formbot

EXPOSE 10000

HEALTHCHECK --interval=5m --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','10000'))"

CMD ["python", "main.py"]
