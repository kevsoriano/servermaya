# Seller Evidence Assistant

An assistant for small sellers who take orders in Messenger and get paid by GCash. It turns a chat into an order, then checks every claim before the seller ships or refunds: payments, packing, delivery, and customer complaints. Each check shows what was claimed, what was verified, and what is still missing.

**Live app:** https://servermaya-ai-builder-cup.web.app

## Try it

Open the live app and press **Try the demo**. No account is needed.

- You get four demo chats, each showing something the app is built to catch. You play both sides: reply as the seller, or write as the customer in the dashed box (images too).
  - **Ana Reyes**: a plain order, for the full flow from draft to delivery.
  - **Mark Dizon**: changes the order, then says he already paid and gives a reference number. That is a claim, not a payment.
  - **Joy Santos**: writes a fake seller line with a lower price, and an instruction to mark the order paid. The seller's price wins and the instruction is ignored.
  - **Leah Cruz**: a returning customer. She confirmed and then cancelled an order three days ago, and now orders again with "same address and number as before".
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
| **Checks before shipping** | Gemini flags specific things in the chat to verify, such as "says they already paid" or pressure to ship before payment. Each check quotes the message. It is never a score or a label on the customer. |
| **Customer confirmation** | The customer gets a link to confirm the order and pay through PayMongo, or by sending GCash directly. |
| **Payment** | A PayMongo webhook with a verified signature is the only thing that marks a payment as verified. For direct GCash, a customer's screenshot is transcribed by Gemini and checked in code (recipient name and number, amount, date, reference format). It is still only a claim until the seller finds it in their own GCash history, either by screenshot or by uploading the GCash history PDF, which is parsed in code with running-balance checks. If the customer says they paid before being asked, receipts count from the start of the conversation about the order instead of from the request. |
| **Packing** | The seller photographs the packed items. Gemini reports what it can see per order line; code compares that with the confirmed order, and anything uncertain waits for the seller. |
| **Dispatch and delivery** | Booking through Lalamove (sandbox), with status from its signed webhook, or a seller-reported courier. Every delivery event is labelled with its source, and "delivered" (the courier) is kept apart from "received" (the customer). |
| **Claims** | The customer reports a problem with photos. Gemini reviews an evidence pack (chat, payment, packing photos, delivery events) and recommends an outcome, citing evidence IDs. Code drops any citation that doesn't exist and holds back findings left without one. |
| **Refunds and replacements** | Separate, explicit seller actions after a decision: a PayMongo API refund, a recorded manual refund, or a replacement shipment. Old records are kept, never overwritten. |
| **Patterns** | Store-wide cards such as a payment reference reused across orders, or repeated claims. Each card shows its counts, the rest of the store for comparison, and its limitations. Cards never block anything. |

## Design rules

- **Gemini transcribes and cites; code decides.** Every pass/fail result, gate and money decision is made in Python. Nothing runs because the AI recommended it.
- **Only the seller sets a price.** An extracted price is kept only if a seller message actually states that number. Otherwise it is left blank for the seller to type.
- **Chat text is data, not instructions.** Customer, courier and model-written text is flattened to one line before it goes into a prompt, so it can't pose as a seller message or a payment record. Prompt injection has a unit test and a live Gemini eval (fake seller lines, a `</chat>` breakout, "ignore instructions", a fake payment record, a Tagalog instruction).
- **Records are appended.** Replaced shipments, re-packing and refunds keep their history.

## How it uses Google Cloud

| Service | Use |
|---|---|
| **Gemini** (via Vertex AI on Agent Platform) | Six structured-output calls: chat-to-order extraction, checks before shipping, packing photo review, GCash receipt transcription, GCash history screenshot transcription, and the claim evidence review. |
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
  main.py         routes, webhooks, Messenger, PayMongo and Lalamove clients
  extraction.py   chat to order, price check, prompt-text flattening
  signals.py      checks before shipping
  orders.py       order versions, confirmation and payment rules
  receipts.py     GCash receipt, history screenshot and history PDF checks
  packing.py      packing photo checks
  delivery.py     dispatch gate and delivery events
  claims.py       evidence pack and claim review
  refunds.py      refunds
  patterns.py     store-wide pattern cards
  demo.py         demo chats, including the returning customer's earlier order
  samples.py      sample GCash receipts, history screenshot and history PDF for demo chats
  test_main.py    tests
  eval_injection.py  live prompt-injection eval
frontend/   React + Vite on Firebase Hosting
docs/       product requirements and implementation plan
```

## Running locally

Needs Python 3.13 with [uv](https://docs.astral.sh/uv/), Node, and Google application-default credentials for a project with Vertex AI and Firestore.

```
make setup   # install dependencies, create backend/.env from .env.example
make dev     # backend on :8000, frontend on :5173
make test    # backend tests, frontend lint and build
make eval-injection   # live Gemini prompt-injection cases
```

Fill in `backend/.env` with your own keys. It is ignored by git.

## Roadmap

Extraction works on a normalized message list; Messenger is the first adapter. Next: Instagram DMs (same Meta app and Graph API), then Facebook and Instagram comments (moving "mine" and price questions into DMs), then WhatsApp.

## Team

- **Kevin Gil Soriano**, Lead Full Stack Engineer
- **Jan Beatrix Escarda**, Full Stack Engineer
