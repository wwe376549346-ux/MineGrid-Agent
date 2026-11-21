# syntax=docker/dockerfile:1

FROM node:22-slim AS frontend-builder
WORKDIR /build/workbench
COPY 研究实施/老洼地数据接入_20261119/workbench/package.json 研究实施/老洼地数据接入_20261119/workbench/package-lock.json ./
RUN npm ci
COPY 研究实施/老洼地数据接入_20261119/workbench/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAOWADI_HOST=0.0.0.0 \
    LAOWADI_PORT=8765

WORKDIR /app

COPY 公开数据与标准_20261113/核验记录/煤矿安全规程_2025_全文.txt /app/公开数据与标准_20261113/核验记录/煤矿安全规程_2025_全文.txt
COPY 项目知识库/资料/老洼地/ /app/项目知识库/资料/老洼地/
COPY 研究实施/大模型接入_20261115/client.py 研究实施/大模型接入_20261115/.env.example /app/研究实施/大模型接入_20261115/
COPY 研究实施/老洼地数据接入_20261119/ /app/研究实施/老洼地数据接入_20261119/
COPY --from=frontend-builder /build/workbench/dist/ /app/研究实施/老洼地数据接入_20261119/workbench/dist/

WORKDIR /app/研究实施/老洼地数据接入_20261119
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -e .

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"

CMD ["python", "-B", "-m", "laowadi", "workbench", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--external-workers", "1"]
