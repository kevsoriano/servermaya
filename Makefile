PROJECT := servermaya-ai-builder-cup
REGION  := asia-southeast1
# The backend's own identity on Cloud Run, with only the roles it needs (runtime-sa). Not the default compute account, which has Editor.
RUN_SA  := api-runtime@$(PROJECT).iam.gserviceaccount.com
EVIDENCE_BUCKET := $(PROJECT)-evidence
# 1 keeps one instance warm through judging; deploy with MIN_INSTANCES=0 afterwards to scale to zero again
MIN_INSTANCES ?= 1
# read from backend/.env, but sent to Cloud Run through Secret Manager instead of as plain env vars
SECRETS := MESSENGER_PAGE_TOKEN PAYMONGO_SECRET_KEY PAYMONGO_WEBHOOK_SECRET LALAMOVE_API_KEY LALAMOVE_API_SECRET
# sent the same way when set; while empty, the Messenger webhook (or the Maps address lookup) stays off
# MAPS_BROWSER_KEY and MAPS_MAP_ID reach every browser through /api/me; they are kept here only so no key sits in plain env vars
OPTIONAL_SECRETS := MESSENGER_APP_SECRET MESSENGER_VERIFY_TOKEN MAPS_SERVER_KEY MAPS_BROWSER_KEY MAPS_MAP_ID

comma := ,
empty :=
space := $(empty) $(empty)

.PHONY: help setup dev dev-backend dev-frontend test eval memory-bank deploy deploy-backend deploy-frontend secrets runtime-sa messenger-webhook

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
	@echo "make runtime-sa       create the backend's service account and grant its roles (deploy-backend runs it)"
	@echo "make messenger-webhook subscribe the Page to the Messenger webhook's message events (once, after it is set up in Meta)"

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
deploy-backend: runtime-sa secrets
	gcloud run deploy api --source backend --project $(PROJECT) --region $(REGION) \
		--service-account $(RUN_SA) --allow-unauthenticated --min-instances $(MIN_INSTANCES) \
		--set-env-vars "^;^$$(grep -v '^#' backend/.env | grep . | grep -v $(patsubst %,-e ^%=,$(SECRETS) $(OPTIONAL_SECRETS)) | paste -sd';' -)" \
		--set-secrets "$$(for k in $(SECRETS) $(OPTIONAL_SECRETS); do grep -q "^$$k=." backend/.env && printf '%s=%s:latest,' $$k $$k; done | sed 's/,$$//')"

# Firestore, the evidence bucket's objects, Vertex AI (Gemini, the claim agent, Memory Bank). Each secret is granted to it
# on its own by `secrets`. Add roles/modelarmor.user here if MODEL_ARMOR_TEMPLATE is set.
runtime-sa:
	@gcloud iam service-accounts describe $(RUN_SA) --project $(PROJECT) >/dev/null 2>&1 || \
		gcloud iam service-accounts create api-runtime --project $(PROJECT) --display-name "Cloud Run backend (api)"
	@for r in roles/datastore.user roles/aiplatform.user roles/aiplatform.memoryUser; do \
		gcloud projects add-iam-policy-binding $(PROJECT) --member serviceAccount:$(RUN_SA) --role $$r --condition None --quiet >/dev/null || exit 1; \
	done
	@gcloud storage buckets add-iam-policy-binding gs://$(EVIDENCE_BUCKET) --member serviceAccount:$(RUN_SA) --role roles/storage.objectUser --quiet >/dev/null

# adds a new version only when the value in backend/.env changed; values are never echoed
secrets: backend/.env
	gcloud services enable secretmanager.googleapis.com --project $(PROJECT)
	@for k in $(SECRETS) $(OPTIONAL_SECRETS); do \
		v=$$(grep "^$$k=" backend/.env | cut -d= -f2-); \
		if [ -z "$$v" ]; then case " $(OPTIONAL_SECRETS) " in *" $$k "*) continue;; esac; echo "$$k is empty in backend/.env"; exit 1; fi; \
		gcloud secrets describe $$k --project $(PROJECT) >/dev/null 2>&1 || \
			gcloud secrets create $$k --project $(PROJECT) --replication-policy automatic --quiet || exit 1; \
		[ "$$(gcloud secrets versions access latest --secret $$k --project $(PROJECT) 2>/dev/null)" = "$$v" ] || \
			printf '%s' "$$v" | gcloud secrets versions add $$k --project $(PROJECT) --data-file=- || exit 1; \
		gcloud secrets add-iam-policy-binding $$k --project $(PROJECT) \
			--member serviceAccount:$(RUN_SA) --role roles/secretmanager.secretAccessor --quiet >/dev/null || exit 1; \
	done

# AI actions go straight to Cloud Run's own URL, past Hosting's 60-second limit on forwarded requests (frontend/src/lib/firebase.ts)
deploy-frontend:
	cd frontend && VITE_API_URL=$$(gcloud run services describe api --project $(PROJECT) --region $(REGION) --format 'value(status.url)') npm run build
	npx firebase-tools deploy --only hosting,firestore:rules --project $(PROJECT)

# Page subscription: Meta sends this Page's messages and quick-reply taps to /api/webhooks/messenger. Set the callback URL and
# verify token in the Meta app's Messenger settings first. The token is read from backend/.env and never echoed.
messenger-webhook: backend/.env
	@id=$$(grep '^MESSENGER_PAGE_ID=' backend/.env | cut -d= -f2-); tok=$$(grep '^MESSENGER_PAGE_TOKEN=' backend/.env | cut -d= -f2-); \
	curl -sS -X POST "https://graph.facebook.com/$$id/subscribed_apps" -H "Authorization: Bearer $$tok" \
		-d "subscribed_fields=messages,messaging_postbacks"; echo
