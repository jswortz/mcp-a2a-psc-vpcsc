FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

ENV PORT=8080
EXPOSE 8080

CMD ["python3", "-m", "mcp_a2a_psc_vpcsc.cloud_run_entrypoint"]
