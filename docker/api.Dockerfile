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
# which commit this image was built from (set by scripts/ and the GitHub workflow); shown by /api/v1/system and /admin
ARG GIT_SHA=dev
ARG BUILD_DATE=
ENV APP_GIT_SHA=$GIT_SHA APP_BUILD_DATE=$BUILD_DATE
LABEL org.opencontainers.image.revision=$GIT_SHA org.opencontainers.image.created=$BUILD_DATE
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
