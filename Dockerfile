FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY trials ./trials
RUN pip install --no-cache-dir .
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "-m", "trials", "serve"]
