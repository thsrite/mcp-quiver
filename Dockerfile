FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 非 root 运行：本服务只做出站 HTTP 抓取，不需要任何特权
RUN useradd -m -u 10001 app && chown -R app:app /app
USER app

# 本镜像只跑 hotcontent（HTTP）；docx-comments 是 stdio，由客户端直接拉起，不需要容器
EXPOSE 8931
# 用 TCP 探测而非 HTTP GET：MCP 端点对裸 GET 返回 406 Not Acceptable（它要
# Accept: text/event-stream），urlopen 会抛 HTTPError 导致容器永远 unhealthy。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,socket,sys;s=socket.socket();s.settimeout(4);sys.exit(s.connect_ex(('127.0.0.1',int(os.environ.get('PORT','8931')))))"

CMD ["mcp-hotcontent"]
