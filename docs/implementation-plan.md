# Seller Evidence Assistant — Implementation Plan

Source: [seller-evidence-workflow-prd.md](seller-evidence-workflow-prd.md) v0.5. Constraints: [submission-requirements.md](submission-requirements.md).

Section numbers in brackets (e.g. [6.3]) refer to the PRD.

## 1. What decides the plan

The submission rules fix three things the PRD leaves open:

- **Stack is mandated**: Firebase, Firestore, Cloud Run, and Gemini must all be used and described.
- **Judges use the deployed prototype unattended.** If an integration is down at evaluation time there is no retry. Every external dependency therefore needs a labeled fallback that keeps the full story clickable.
- **The video is 3–4 minutes** and must show working functionality. The eight-step demo in [14] is the product; anything not on that path is cut first.

## 2. Stack

| Concern | Choice | Why |
| --- | --- | --- |
| Frontend | React + Vite + TypeScript in `frontend/`, on Firebase Hosting | Static build; Hosting rewrites `/api/**` to the backend, so one origin and no CORS |
| Backend | FastAPI (Python, `uv`) in `backend/`: API and webhooks, on Cloud Run service `api` in `asia-southeast1`, `min-instances=1` during evaluation | Mandated; avoids cold-start webhook timeouts |
| Database | Firestore, accessed only from the backend | Mandated; security rules deny all client access, so store isolation is enforced in one place |
| Auth | Firebase Auth with Google sign-in; backend verifies the ID token and checks `ALLOWLIST_EMAIL`. Roles (`owner`, `staff`) come later as a custom claim | Mandated family; covers role-based access [5] |
| Files | Cloud Storage for Firebase | Packing photos, claim photos, original uploads |
| AI | Gemini through the `google-genai` Python SDK with JSON-schema structured output | Mandated; one model handles text, images, and audio, so no separate vision or speech provider is needed [16] |
| Payments | PayMongo sandbox, Hosted Checkout with QR Ph | Committed path [6.3] |
| Delivery | Lalamove sandbox, PH market | Planned path [6.5] |
| Secrets | Secret Manager, mounted as Cloud Run env vars | Keys stay server-side |
| Tests | `pytest` on the pure domain modules | Already installed |

The Gemini model ID lives in one env var. Use the current Flash-tier model for extraction and packing checks; switch to Pro only if evaluation shows it is needed.

Both run in the one project `servermaya-ai-builder-cup`.

Not used: a queue, a vector database, an ORM, a state-machine library. Add a queue only if synchronous Gemini calls make requests too slow.

## 3. Decisions taken on the PRD's open questions [16]

| Open question | Default taken here | Revisit when |
| --- | --- | --- |
| Customer confirmation: chat reply or signed link | Signed link to a public confirmation page. It also carries the checkout URL and later the receipt acknowledgment and claim form | Never for MVP |
| Messenger ingestion | Pasted text and typed notes first. Messenger webhook second, in Meta development mode with tester accounts only (no app review) | Spike S4 fails: ship pasted text only and say so |
| AI providers | Gemini for everything | Evaluation results are poor |
| Packing narration | Short recorded clip sent to Gemini as audio with the photos; discarded after review | Cut entirely if Phase 5 runs late; typed notes cover it |
| Delivery-fee payer | Seller-paid. The customer total never changes after confirmation, so no reconfirmation loop | Never for MVP |
| Shipments per order | One, plus linked replacement shipments. Stated in the interface [5] | Never for MVP |
| Minimum sample for rate comparisons | 10 in the denominator; below that the card says "insufficient history" | Evaluation |
| Shopee | Not built. Research only if time remains after Phase 10 | Post-hackathon |

## 4. Architecture

```
Browser
   │
Firebase Hosting
   ├─ frontend/ (React)    seller UI: orders, order detail, claim review, patterns
   │    └─ /c/:token       public customer pages: confirm, pay, acknowledge, claim
   └─ /api/** ──► Cloud Run service `api` (FastAPI, backend/)
        ├─ /api/...             authenticated routes (Firebase ID token + allowlist)
        ├─ /api/webhooks/...    paymongo, lalamove, messenger
        ├─ domain/              pure: states, gates, refund balance, patterns   ← tested
        ├─ adapters/            paymongo, lalamove, messenger, gemini, storage
        └─ repo/                Firestore reads and writes, always scoped by store
   │
Firestore · Cloud Storage · Gemini · PayMongo · Lalamove
```

Rules that hold everywhere:

- **Gates and money are deterministic.** State transitions, the dispatch gate, and refund balances live in `backend/domain` as pure functions. Gemini output never changes a state; it only produces insights the seller reads [7].
- **Every fact is an evidence event.** Adapters append to `evidence`; nothing is edited or deleted. Normalized records (payment, fulfillment) are derived from and link back to those events [10].
- **Webhooks are idempotent by construction.** The provider event ID is the Firestore document ID in `webhookEvents`; `create()` fails on a duplicate. State updates run in a transaction that rejects regressions using the provider timestamp [6.3, 12].
- **AI runs inside the request that needs it**, not in the background. Cloud Run does not guarantee CPU after the response is sent. A webhook only records the event and marks affected insights stale; the insight is regenerated when the seller opens the view.
- **AI output is validated before display.** It must match the schema, and every cited evidence ID must exist and belong to the same order. Otherwise the finding is withheld and shown as "insufficient data" [12].
- **Customer text is data.** It goes into the prompt inside a delimited block, with instructions in the system prompt only. No tool calling is exposed to the model.

## 5. Data model (Firestore)

Everything sits under `stores/{storeId}` except webhook dedupe. Amounts are integer centavos.

| Collection | Key fields |
| --- | --- |
| `orders` | `currentVersion`, `status` (draft, awaiting_confirmation, confirmed, cancelled), `customerRef`, `isDemo` |
| `orders/{id}/versions` | Immutable: line items, totals, address, `confirmation` {source, timestamp, kind: customer or seller_asserted} |
| `payments` | `orderId`, `orderVersion`, checkout session ID, provider payment ID, amount, `status` [6.3 states], `lastVerifiedAt`, `refundedAmount`, `pendingRefundAmount`, `livemode` |
| `fulfillments` | `orderId`, `orderVersion`, `replacesFulfillmentId`, `status`, packing checklist rows, Lalamove order ID, quotation, share link, raw provider status, POD |
| `receipts` | `fulfillmentId`, `status` (unconfirmed, customer_confirmed), source, timestamp |
| `claims` | `orderId`, `fulfillmentId`, `type`, `status` [9], narrative, owner, next action |
| `evidence` | `orderId`, `sourceType` (provider, seller_reported, customer_reported, demo), original reference, `sha256`, `uploadedAt`, `claimedCapturedAt`, payload |
| `insights` | `subject`, findings with evidence IDs, model ID, `state` (current, stale, superseded) |
| `decisions` | approver, approved order version, action, reason, `operationId`, execution status |
| `refunds` | `paymentId`, `decisionId`, amount, provider refund ID, `status` [6.8 states] |
| `patternFindings` | rule and version, window, entity IDs, incident IDs, numerator, denominator, baseline, completeness, review status and reason |
| `/webhookEvents` (top level) | Document ID is `{provider}_{eventId}`; raw body, received time |

A packing checklist row holds: expected, visually observed, seller-reported, source references, and result (matched, conflict, insufficient_visibility, seller_confirmed) [6.4].

## 6. Phases

Each phase ends deployed to Cloud Run and usable. Phases 1–8 are the PRD's P0; 9–11 are P1.

### Phase 0 — Sandbox spikes (do first, half a day)

These decide which fallbacks are needed. Record each result in the README.

| Spike | Question | If it fails |
| --- | --- | --- |
| S1 PayMongo | Does a Checkout Session with QR Ph work in test mode? What exactly is in `checkout_session.payment.paid`? How is the signature header built? Can a QR Ph test payment be refunded, fully and partially? | Try the Payment Intent QR Ph flow [6.3]. If refunds cannot be exercised, seed labeled refund outcomes |
| S2 Lalamove | Do sandbox credentials work for PH? Can a quote be booked? Do status webhooks arrive, and can statuses be advanced? Is POD returned? | Use the labeled delivery simulator (Phase 6) as the main path |
| S3 Gemini | Does structured output return reliable line items from a Taglish chat, and spot a wrong-colour item in a photo? | Tighten prompts; move to the Pro tier |
| S4 Messenger | Can a development-mode app receive Page messages from a tester account? | Pasted text only |

### Phase 1 — Skeleton and deploy

- Done: React app with Google sign-in, FastAPI with the `current_user` dependency and email allowlist, Dockerfile, `Makefile` (`make setup`, `make dev`, `make deploy`).
- Secret Manager wiring; a store-scoping helper used by every route.
- Firestore rules that deny all client access.
- Seed script: one store, a demo seller login, synthetic customers and order history. Every seeded record has `isDemo: true`.
- Order list page reading from Firestore.

Done when: the public URL shows a login and a seeded order list.

### Phase 2 — Domain core

Pure Python in `backend/domain`, with tests:

- State machines for each entity in [9], including cancel and reopen.
- `canDispatch(order, payment, fulfillment)`: current version confirmed, full amount verified, packing complete [6.3].
- `onNewVersion()`: invalidates confirmation, payment request, and packing checks that depend on the old version [6.2].
- `refundable(payment)`: amount minus succeeded and pending refunds [6.8].
- `applyProviderEvent()`: ignores duplicates and older events.

Done when: tests cover each acceptance criterion in [13] that concerns gates, versions, and duplicate handling.

### Phase 3 — Conversation to confirmed order [6.1, 6.2]

- Paste box and seller-note box on a new-order screen. Each input is stored as evidence with its source type.
- Gemini extraction to a schema: items, variants, quantities, prices, recipient, address, claims, plus a `sourceQuote` per field and a list of clarification questions. Missing fields stay empty.
- Editable draft; saving creates version 1. Any later edit to quantity, price, item, address, or payment instructions creates a new version.
- "Send for confirmation" produces a signed link. The customer page shows the summary and a Confirm button; confirming records version, source, and time.
- "Customer agreed verbally" is a separate seller action with its own status.
- Order detail page: stage tracker, current version, evidence timeline, missing-information checklist, action panel [8].

### Phase 4 — Payment verification [6.3]

- "Request payment" creates a PayMongo Checkout Session for the confirmed version's total, with the order ID and version in metadata. The link appears on the customer page.
- Webhook route: verify signature against the raw body, dedupe, fetch the payment from PayMongo, reconcile amount, currency, and order version, then set the state.
- The success-redirect page says "waiting for confirmation from PayMongo" and changes nothing.
- A new order version retires the old session and requires a new one. A second successful payment is recorded and routed to review as an overpayment.
- "Customer says they paid" is a button that records a customer-reported claim and shows "no matching verified payment" beside it. A screenshot upload is stored as evidence only.
- "Recheck" button retrieves the session from PayMongo; failure sets `verification_unavailable`, which blocks dispatch.
- Test badge on every non-live payment.

### Phase 5 — Packing checklist and vision check [6.4]

- Checklist generated from the confirmed version's line items.
- Photo upload through the backend: store the original, compute SHA-256, record upload time separately from any claimed capture time.
- Gemini compares photos with the checklist and returns one result per row with the photo it relied on. "Insufficient visibility" is a valid answer and never passes automatically.
- Seller resolves each conflict by correcting and re-uploading, or by recording a manual inspection with a reason. Automated matches and manual confirmations are shown differently.
- "Mark packed" is a seller action, enabled only when every row is matched or seller-confirmed.
- Narration (P1): a visible record button using the browser's `MediaRecorder`; the clip goes to Gemini with the photos; spoken values fill the seller-reported column; audio is not stored; tracks are stopped on Stop. Photo upload works with no microphone permission.

### Phase 6 — Dispatch and delivery [6.5]

- Lalamove adapter: signed requests, quotation, order placement with POD requested, order retrieval.
- Seller sees the quote and approves it. Booking is enabled only when `canDispatch` passes.
- Before any booking retry, retrieve by our fulfillment ID to avoid a duplicate order.
- Webhook route: verify, dedupe, store raw status, map to normalized state. Booking or driver assignment does not mean picked up.
- "Refresh status" button calls order retrieval. Share link and POD are shown with source and time.
- **Delivery simulator**: a panel that appends status events with source type `demo` and a visible "Simulated" badge. It is the fallback if S2 fails and the tool for demo steps the sandbox cannot produce.
- Customer page gains "I received my order" and "Report a problem". Delivered and customer-confirmed stay separate fields.

### Phase 7 — Claims and evidence review [6.6, 6.7]

- Claim form on the customer page and in the seller app: type, narrative, photos. Closed orders can be reopened by a claim.
- Case view: the claim beside the confirmed version, messages, payment record, packing evidence, and delivery events.
- Gemini produces findings, each one of supported, conflicting, unverified, or insufficient data, with evidence IDs, plus a recommended action and a draft customer reply. Both are editable.
- Seller approves an outcome; this writes a decision record. Approval does not execute anything.
- Replacement creates a new fulfillment linked to the original and re-enters Phase 5.

### Phase 8 — End-to-end pass (P0 complete)

Run demo steps 1–6 and 8 of [14] on the deployed URL with a fresh order. Fix what breaks. Do not start Phase 9 before this passes.

### Phase 9 — Refund execution [6.8]

- From an approved decision: choose payment, amount, and reason; the screen shows the remaining refundable amount. A second explicit confirmation submits.
- A transaction reserves the amount as pending and writes an operation ID before the PayMongo call. A timeout leaves the refund "submitted"; "Check status" reconciles before any retry is offered.
- Refund webhooks move pending to succeeded or failed. API acceptance alone is shown as "submitted".
- If the API refuses: show the manual route (PayMongo Dashboard), then reconcile. A manually reported transfer is labeled seller-reported, never provider-verified.

### Phase 10 — Pattern analysis [6.7a]

- Pure functions in `backend/domain/patterns.py` over store records: payment-reference reuse, claims per fulfilled order per customer, damage and nonreceipt reports per delivered shipment by courier and by SKU. Each returns numerator, denominator, baseline, incident IDs, and completeness.
- Seed data for the three scenarios and their three counterexamples: a combined payment explicitly allocated to two orders, a high-volume courier, and a defective product across several customers.
- Tests assert counts against hand-checked fixtures, that duplicate reports of one incident do not inflate rates, and that a missing rider ID disables rider-level grouping.
- Gemini writes only the similarity summary and suggested questions for a card, from the incident IDs it is given.
- Patterns page with filters; cards also shown on related orders and claims. Review statuses with a required reason. Recomputed on page load.
- Cards never disable any action.

### Phase 11 — Evaluation, hardening, submission

- Evaluation script: 20 synthetic conversation and claim cases and 12 packing cases [13]. Output a results table (field accuracy, discrepancies caught, false matches, citation validity) and commit it.
- Prompt-injection cases in the evaluation set: chat text that tells the model to mark the order paid.
- Access check: a second seeded store; confirm no route returns its data.
- Judge path: demo login shown on the sign-in page, a "Reset demo data" button, and seeded orders at each stage so any screen can be reached without completing a payment.
- Pitch deck in the prescribed template, the 3–4 minute video following [14], and the description of how Firebase, Firestore, Cloud Run, and Gemini are used.

## 7. If time runs short

Cut in this order. Each cut keeps the demo story intact.

1. Messenger webhook (pasted text remains).
2. Packing narration (typed notes remain).
3. Live Lalamove booking (simulator remains, labeled).
4. Partial refunds (full refund only).
5. Two of the three pattern scenarios (keep damage concentration, which is demo step 7).
6. Live refund execution (seeded, labeled outcome).

Do not cut: the PayMongo verified-payment webhook, the dispatch gate, the packing vision check, and the claim review with citations. Those are the product.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| A sandbox is down while judges evaluate | Seeded orders at every stage; labeled simulator; the app shows "verification unavailable" instead of failing |
| PayMongo webhook payload differs from the docs | Spike S1 before writing the handler; store the raw body |
| Judges cannot complete a QR Ph test payment themselves | Seeded verified-payment order, plus a short "how to pay in test mode" note on the checkout step |
| Gemini returns uncited or wrong findings | Citation validation withholds them; the seller edits everything; results are reported honestly from the evaluation run |
| Meta permissions block Messenger | Development mode with testers only; otherwise cut |
| Scope | Phase 8 gate before any P1 work |

## 9. Needed before starting

- Submission deadline and team size, to turn the phases into dates and owners.
- A Google Cloud project with billing, a PayMongo test account, Lalamove Partner Portal sandbox access, and a Meta developer app with a test Page.
