PROJECT := servermaya-ai-builder-cup
REGION  := asia-southeast1
RUN_SA  := 628763008062-compute@developer.gserviceaccount.com
# 1 keeps one instance warm through judging; deploy with MIN_INSTANCES=0 afterwards to scale to zero again
MIN_INSTANCES ?= 1
# read from backend/.env, but sent to Cloud Run through Secret Manager instead of as plain env vars
SECRETS := MESSENGER_PAGE_TOKEN PAYMONGO_SECRET_KEY PAYMONGO_WEBHOOK_SECRET LALAMOVE_API_KEY LALAMOVE_API_SECRET

comma := ,
empty :=
space := $(empty) $(empty)

.PHONY: help setup dev dev-backend dev-frontend test eval memory-bank deploy deploy-backend deploy-frontend secrets

help:
	@echo "make setup            install dependencies, create backend/.env"
	@echo "make dev              run backend (:8000) and frontend (:5173)"
	@echo "make test             backend lint, format check and tests; frontend lint, format check and build"
	@echo "make eval             live Gemini evaluation; writes docs/eval-results.md"
	@echo "make memory-bank      create the Memory Bank for past claim decisions (once), then write its demo examples"
	@echo "make deploy           deploy backend to Cloud Run, then frontend to Firebase Hosting"
	@echo "make deploy-backend   backend only"
	@echo "make deploy-frontend  frontend only"
	@echo "make secrets          copy the keys in backend/.env to Secret Manager (deploy-backend runs it)"

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
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
	cd frontend && npm run lint && npm run format:check && npm run build

eval:
	cd backend && uv run python -m evals.run $(or $(RUNS),3)

memory-bank: backend/.env
	cd backend && uv run --env-file .env python -m integrations.memory setup

deploy: deploy-backend deploy-frontend

# ^;^ switches gcloud's list delimiter to ';' so the comma-separated allowlist survives
deploy-backend: secrets
	gcloud run deploy api --source backend --project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated --min-instances $(MIN_INSTANCES) \
		--set-env-vars "^;^$$(grep -v '^#' backend/.env | grep . | grep -v $(patsubst %,-e ^%=,$(SECRETS)) | paste -sd';' -)" \
		--set-secrets "$(subst $(space),$(comma),$(foreach k,$(SECRETS),$(k)=$(k):latest))"

# adds a new version only when the value in backend/.env changed; values are never echoed
secrets: backend/.env
	gcloud services enable secretmanager.googleapis.com --project $(PROJECT)
	@for k in $(SECRETS); do \
		v=$$(grep "^$$k=" backend/.env | cut -d= -f2-); \
		[ -n "$$v" ] || { echo "$$k is empty in backend/.env"; exit 1; }; \
		gcloud secrets describe $$k --project $(PROJECT) >/dev/null 2>&1 || \
			gcloud secrets create $$k --project $(PROJECT) --replication-policy automatic --quiet || exit 1; \
		[ "$$(gcloud secrets versions access latest --secret $$k --project $(PROJECT) 2>/dev/null)" = "$$v" ] || \
			printf '%s' "$$v" | gcloud secrets versions add $$k --project $(PROJECT) --data-file=- || exit 1; \
		gcloud secrets add-iam-policy-binding $$k --project $(PROJECT) \
			--member serviceAccount:$(RUN_SA) --role roles/secretmanager.secretAccessor --quiet >/dev/null || exit 1; \
	done

deploy-frontend:
	cd frontend && npm run build
	npx firebase-tools deploy --only hosting,firestore:rules --project $(PROJECT)
