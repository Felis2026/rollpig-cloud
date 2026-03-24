FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir poetry && poetry config virtualenvs.create false && poetry install --no-interaction --no-ansi --no-root

COPY rollpig_cloud ./rollpig_cloud
COPY tools ./tools

EXPOSE 8011

CMD ["uvicorn", "rollpig_cloud.main:app", "--host", "0.0.0.0", "--port", "8011"]
