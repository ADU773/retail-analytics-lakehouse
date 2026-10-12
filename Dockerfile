# Laptop-scale lakehouse in a container: runs the full pipeline by default.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY app ./app
RUN pip install ".[dashboard]"

RUN useradd --create-home --uid 1000 lakehouse \
    && mkdir -p /app/data /app/reports /app/docs \
    && chown -R lakehouse:lakehouse /app
USER lakehouse

EXPOSE 8501

# Default: build the whole lakehouse at a container-friendly scale.
# Dashboard: docker run -p 8501:8501 <image> streamlit run app/dashboard.py --server.address=0.0.0.0
CMD ["retail-lakehouse", "run-all", "--scale", "small"]
