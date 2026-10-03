FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY shuttle ./shuttle
COPY app ./app
RUN pip install --no-cache-dir .

# Real operation by default; each day's actions are kept in /data so a restart resumes the day.
ENV SHUTTLE_MODE=real \
    SHUTTLE_DATA_DIR=/data \
    FORWARDED_ALLOW_IPS="*" \
    PORT=8000
VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "app"]
