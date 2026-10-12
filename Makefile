PROJECT := servermaya-ai-builder-cup
REGION  := asia-southeast1

.PHONY: help setup dev dev-backend dev-frontend test deploy deploy-backend deploy-frontend

help:
	@echo "make setup            install dependencies, create backend/.env"
	@echo "make dev              run backend (:8000) and frontend (:5173)"
	@echo "make test             backend tests, frontend lint and build"
	@echo "make deploy           deploy backend to Cloud Run, then frontend to Firebase Hosting"
	@echo "make deploy-backend   backend only"
	@echo "make deploy-frontend  frontend only"

setup: backend/.env
	cd backend && uv sync
	cd frontend && npm install

backend/.env:
	cp backend/.env.example $@
	@echo "Created $@ - set ALLOWLIST_EMAIL"

dev:
	@$(MAKE) -j2 dev-backend dev-frontend

dev-backend: backend/.env
	cd backend && uv run --env-file .env uvicorn main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest -q
	cd frontend && npm run lint && npm run build

deploy: deploy-backend deploy-frontend

# ^;^ switches gcloud's list delimiter to ';' so the comma-separated allowlist survives
deploy-backend: backend/.env
	gcloud run deploy api --source backend --project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated \
		--set-env-vars "^;^$$(grep -v '^#' backend/.env | grep . | paste -sd';' -)"

deploy-frontend:
	cd frontend && npm run build
	npx firebase-tools deploy --only hosting --project $(PROJECT)
