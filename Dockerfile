FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx prometheus-client
COPY src ./src
EXPOSE 18083
CMD ["python", "-m", "portable_runtime", "--state", "/data/portable-runtime.db", "status"]
