# ── 1단계: 프론트엔드 빌드 ───────────────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ── 2단계: 백엔드 실행 ────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend /frontend/dist ./frontend_dist

# 데이터 디렉터리 (Railway 볼륨 마운트 대상)
RUN mkdir -p /data/uploads
ENV DATA_DIR=/data

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
