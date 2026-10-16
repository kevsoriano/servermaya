# Seller Evidence Assistant

An assistant for small sellers who take orders in Messenger and get paid by GCash. It turns a chat into an order, then checks every claim before the seller ships or refunds: payments, packing, delivery, and customer complaints. Each check shows what was claimed, what was verified, and what is still missing.

**Live app:** https://servermaya-ai-builder-cup.web.app

## Try it

Open the live app and press **Try the demo**. No account is needed.

- You get seven demo chats, each showing something the app is built to catch. You play both sides: reply as the seller, or write as the customer in the dashed box (images too).
  - **Ana Reyes**: a plain order, for the full flow from draft to delivery.
  - **Mark Dizon**: changes the order, then says he already paid and gives a reference number. That is a claim, not a payment.
  - **Joy Santos**: writes a fake seller line with a lower price, and an instruction to mark the order paid. The seller's price wins and the instruction is ignored.
  - **Leah Cruz**: a returning customer. She confirmed and then cancelled an order three days ago, and now orders again with "same address and number as before".
  - **Rico Bautista**: a repeat customer whose first order said "already paid, ship today" but went through: the seller found the payment in their own GCash history, packed it and he received it. Now he says the same again. The old payment does not settle the new claim; the first order's checks show as history.
  - **Bea Villanueva**: her mug was delivered cracked, and she sent a report with a photo. The report is open, so **Review with AI** is one click away; the reviewer sees her photo and the packing photo.
  - **Nico Ramos**: paid and waiting to be packed, with one shaky packing photo already added. **Check photos** holds it back as too blurry and asks for a retake, whatever the AI reads in it.
- From a chat: create the draft order, create the confirmation link, confirm it as the customer, then pay with PayMongo's **Simulate payment** (test mode) or by GCash. Then check packing photos, book a Lalamove sandbox delivery, mark it received, report a problem and review the claim.
- To try GCash, press **Ask for GCash payment** (or **They say they already paid** for Mark). The customer box then has made-up sample receipts: **Matching**, **Wrong amount** and **Wrong recipient**, each stamped SAMPLE. To confirm a payment the way a seller would, use **Sample history screenshot** in the order, or download the sample GCash history PDF in Settings (password `delacruz4567`) and upload it.
- **New demo chat** pastes any chat in `Customer:` / `Seller:` lines. **Reset demo chats** starts over.
- Demo visitors only see their own demo chats and orders, with a made-up GCash account and pickup address. Messenger is live but limited to app testers while the Meta app is in development mode, so real chats are not part of the demo.

## The problem

A small Philippine online seller keeps the order in a Messenger thread, the payment in a GCash screenshot, the packing in their memory and the delivery in a courier app. Nothing ties these together. So the seller can't tell whether a screenshot is a real payment, what was actually agreed after the customer changed their mind, or whether a "missing item" complaint is true. Mistakes cost them goods, wrong refunds and disputes with honest customers.

## What it does

| Stage | What happens |
|---|---|
| **Chat to order** | Gemini reads the Messenger thread and drafts the order: items, quantities, recipient, address. Every field cites the message it came from, and the seller reviews and edits before anything is sent. For a returning customer, the new order is drafted only from the messages after the last order. Name, phone and address the customer doesn't repeat are copied from their last confirmed order and marked as copied, not cited. |
| **Checks before shipping** | Gemini flags specific things in the chat to verify, such as "says they already paid", pressure to ship before payment, or asking the seller to open an account, use a link or share a code to "receive" payment. Each check quotes the message. It is never a score or a label on the customer. |
| **Customer confirmation** | The customer gets a link to confirm the order and pay through PayMongo, or by sending GCash directly. |
| **Payment** | A PayMongo webhook with a verified signature is the only thing that marks a payment as verified. For direct GCash, a customer's screenshot is transcribed by Gemini and checked in code (recipient name and number, amount, date, reference format). It is still only a claim until the seller finds it in their own GCash history, either by screenshot or by uploading the GCash history PDF, which is parsed in code with running-balance checks. If the customer says they paid before being asked, receipts count from the start of the conversation about the order instead of from the request. |
| **Packing** | The seller photographs the packed items. Gemini reports what it can see per order line; code compares that with the confirmed order, and anything uncertain waits for the seller. |
| **Dispatch and delivery** | Booking through Lalamove (sandbox), with status from its signed webhook, or a seller-reported courier. Every delivery event is labelled with its source, and "delivered" (the courier) is kept apart from "received" (the customer). |
| **Claims** | The customer reports a problem with photos. An ADK agent starts from the claim and the customer's photos, fetches the order's records with read-only tools (the order and payment, the chat, packing with its photos, delivery events), recalls how the shop decided similar claims from Memory Bank, and recommends an outcome, citing evidence IDs. None of its tools can change anything. Its after-agent callback runs code that drops any citation to a record it was not given or did not fetch, and holds back findings left without one. |
| **Refunds and replacements** | Separate, explicit seller actions after a decision: a PayMongo API refund, a recorded manual refund, or a replacement shipment. Old records are kept, never overwritten. |
| **Patterns** | Store-wide cards such as a payment reference reused across orders, or repeated claims. Each card shows its counts, the rest of the store for comparison, and its limitations. Cards never block anything. |

## Design rules

- **Gemini transcribes and cites; code decides.** Every pass/fail result, gate and money decision is made in Python. Nothing runs because the AI recommended it.
- **Only the seller sets a price.** An extracted price is kept only if a seller message actually states that number. Otherwise it is left blank for the seller to type.
- **Chat text is data, not instructions.** Customer, courier and model-written text is flattened to one line before it goes into a prompt, so it can't pose as a seller message or a payment record. Prompt injection has a unit test and live Gemini eval cases (fake seller lines, a `</chat>` breakout, "ignore instructions", a fake payment record, a Tagalog instruction, and a fake record in a claim).
- **Records are appended.** Replaced shipments, re-packing and refunds keep their history.

## How it uses Google Cloud

| Service | Use |
|---|---|
| **Gemini** (via Vertex AI on Agent Platform) | Six structured-output calls: chat-to-order extraction, checks before shipping, packing photo review, GCash receipt transcription, GCash history screenshot transcription, and the claim evidence review. |
| **Agent Development Kit (ADK)** | The claim review runs as an ADK `LlmAgent` with five read-only function tools (`read_order`, `read_chat`, `read_packing`, `read_delivery`; packing photos come back as images in the tool result; and `read_past_decisions`, from Memory Bank) and a structured output schema, through an ADK `Runner` with one in-memory session per review. The citation check runs as the agent's after-agent callback, against only what the agent was given or fetched. |
| **Memory Bank** (Agent Platform) | The shop's past claim decisions. When the seller approves an outcome, the backend writes one memory as given, with no AI rewriting: the claim type, the items and the seller's decision and reason, never the customer's words, so nothing a customer writes can be stored and replayed into later reviews. The claim reviewer recalls the closest past decisions by similarity search, scoped to the store (or to one demo visitor, plus shared demo examples), and cites them as P1, P2… The prompt and an eval case keep them as practice, never as evidence about this customer. If Memory Bank is off or down, reviews go on without it. |
| **Cloud Run** | The FastAPI backend: all API routes, the Messenger Graph API client, and the PayMongo and Lalamove webhooks. Scales to zero. |
| **Firebase Hosting** | Serves the React frontend and routes `/api/**` to Cloud Run on the same domain. |
| **Firebase Authentication** | Google sign-in for sellers, checked against an email allowlist, and anonymous sign-in for demo visitors. The backend verifies the ID token on every protected route and limits demo visitors to their own demo chats and orders. |
| **Firestore** | Orders with their full history, claims, refunds, delivery events, settings and webhook dedupe records. Security rules deny all browser access; everything goes through the backend. |
| **Cloud Storage** | Evidence files: packing and claim photos, GCash screenshots, images sent in replies. Stored by content hash. |
| **Secret Manager** | API keys (Messenger, PayMongo, Lalamove) reach Cloud Run as secrets, readable only by the service account. |

Other services: Messenger Graph API (conversations and replies), PayMongo (checkout, webhooks, refunds, in test mode), Lalamove (sandbox quotes, bookings and webhooks).

## Repository

```
backend/    FastAPI on Cloud Run
  main.py           the app: includes one router per area
  routes/           HTTP routes by area: conversations, demo, orders, customer (the customer's link), gcash, packing,
                    dispatch, claims, refunds, patterns, webhooks
  core/             config, auth (sign-in, allowlist, demo gate, demo AI limits) and Firestore/Storage access
  integrations/     Messenger Graph API, PayMongo and Lalamove clients
  services/         shared helpers the routes call: demo chats, demo seeding, the customer link, payment and delivery sync
  domain/           business rules as pure functions, no I/O:
    extraction.py     chat to order, price check, prompt-text flattening
    signals.py        checks before shipping, and which order each check belongs to
    orders.py         order versions, confirmation and payment rules
    receipts.py       GCash receipt, history screenshot and history PDF checks
    packing.py        packing photo checks, including the sharpness check
    delivery.py       dispatch gate and delivery events
    claims.py         evidence pack and claim review
    claim_agent.py    the claim review as an ADK agent: its read-only tools, and the citation check as its callback
    refunds.py        refunds
    patterns.py       store-wide pattern cards
  seed/             demo chats and sample GCash files (receipts, history screenshot and PDF)
  tests/            unit tests by area (make test)
  evals/run.py      live evaluation against Gemini (make eval)
frontend/   React + Vite on Firebase Hosting
  src/App.tsx         sign-in and the four tabs
  src/conversations/  chat list, thread, reply box, checks before shipping, image viewer
  src/orders/         order list and order panel: GCash, packing, dispatch, claims and refunds
  src/settings/       GCash account and pickup address
  src/patterns/       store-wide pattern cards
  src/customer/       the customer's link page: confirm, pay, report a problem
  src/lib/            Firebase and API calls, display formatting, receipt check labels
docs/       product requirements, implementation plan, test scenarios and eval results
```

## Running locally

Needs Python 3.13 with [uv](https://docs.astral.sh/uv/), Node, and Google application-default credentials for a project with Vertex AI and Firestore.

```
make setup   # install dependencies, create backend/.env from .env.example
make dev     # backend on :8000, frontend on :5173
make test    # backend tests, frontend lint and build
make eval    # live Gemini evaluation, writes docs/eval-results.md
```

The demo chats and evaluation cases are described in [docs/test-scenarios.md](docs/test-scenarios.md), with the latest results in [docs/eval-results.md](docs/eval-results.md).

Fill in `backend/.env` with your own keys. It is ignored by git.

## Roadmap

Extraction works on a normalized message list; Messenger is the first adapter. Next: Instagram DMs (same Meta app and Graph API), then Facebook and Instagram comments (moving "mine" and price questions into DMs), then WhatsApp.

## Team

- **Kevin Gil Soriano**, Lead Full Stack Engineer
- **Jan Beatrix Escarda**, Full Stack Engineer
