FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 非 root 运行：本服务只做出站 HTTP 抓取，不需要任何特权
RUN useradd -m -u 10001 app && chown -R app:app /app
USER app

EXPOSE 8931
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8931/mcp',timeout=4)" || exit 1

CMD ["mcp-hotcontent"]
