# The gateway (FastAPI). Built by docker-compose.yml; see docs/USAGE.md.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml ./
RUN mkdir gateway evals && pip install --no-cache-dir . && rm -rf build *.egg-info

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY gateway ./gateway
# Import the code from /app (so config/ and migrations/ resolve next to it).
ENV PYTHONPATH=/app

# Run as a normal user, not root.
RUN useradd --create-home gateway
USER gateway

EXPOSE 8000
# Bring the database schema up to date, then serve. Migrations are safe to rerun, but not
# from several containers starting at once: with more than one gateway, set
# RUN_MIGRATIONS=false and run `alembic upgrade head` once per deploy (docs/DEPLOY.md).
ENV RUN_MIGRATIONS=true
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = true ]; then alembic upgrade head || exit 1; fi; exec uvicorn gateway.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
