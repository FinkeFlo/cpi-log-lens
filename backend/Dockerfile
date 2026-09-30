FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# glibc creates up to 8 malloc arenas per CPU; with DuckDB's worker threads the
# fragmented heap grew far beyond memory_limit. Two arenas keep RSS close to it.
ENV MALLOC_ARENA_MAX=2

EXPOSE 8080
# No --reload here: with the reloader as PID 1, a worker killed by the OOM
# killer leaves the port open but unanswered and the container never restarts.
# uvicorn itself is PID 1, so any crash ends the container and the restart
# policy brings it back. Use compose.dev.yaml for live reload.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "5"]
