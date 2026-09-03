# The API only. The collector runs on the host under a systemd timer.

FROM python:3.12-slim

# Sources publish local wall clock times with no offset, so a timezone database
# is required, not optional. Slim images ship without one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

# Nothing here needs root.
RUN useradd --system --uid 10001 atmos
USER atmos

EXPOSE 8000
CMD ["uvicorn", "atmos.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
