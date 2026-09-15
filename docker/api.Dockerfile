FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libfribidi0 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY apps ./apps
COPY services ./services
COPY workers ./workers
COPY agents ./agents
COPY prompts ./prompts
COPY database ./database
COPY scripts ./scripts
COPY web ./web
RUN pip install --no-cache-dir ".[composer]"
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
