# ---- build the console ----
FROM node:22-alpine AS web
WORKDIR /repo/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
# The simulations page bundles the scenario cards and the recorded run, the engine the Cedar policy, and the policy page the
# journeys it replays; they live outside frontend/: same paths as the repo.
COPY backend/evals/simulations/ /repo/backend/evals/simulations/
COPY backend/evals/journeys.json /repo/backend/evals/journeys.json
COPY backend/policies/ /repo/backend/policies/
COPY demo/simulations/ /repo/demo/simulations/
COPY frontend/ ./
RUN pnpm build

# ---- API + static console in one image ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.2"
COPY backend/ ./
COPY --from=web /repo/frontend/dist /app/frontend/dist
# A numeric user, so Kubernetes can check runAsNonRoot (deploy/k8s runs it as 10001).
RUN useradd --create-home --uid 10001 loopdesk && chown -R loopdesk /app
USER 10001
EXPOSE 8000
# Readiness = database reachable, schema at the latest migration, retrieval indexes warm (see /api/ready).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
