# Suri

An assistant for small sellers who take orders in Messenger and get paid by GCash. It turns a chat into an order, then checks every claim before the seller ships or refunds: payments, packing, delivery, and customer complaints. Each check shows what was claimed, what was verified, and what is still missing.

**Live app:** https://servermaya-ai-builder-cup.web.app

## Try it

Open the live app and press **Try the demo**. No account is needed.

- You get fifteen demo chats. You are the seller; each customer is played by AI (Gemini) and answers your messages on their own, in character, sticking to what their chat already says.
- From a chat: create the draft order and send it for confirmation; the customer taps **Confirm** under the summary in the chat. Send the payment link in the chat and pay with PayMongo's **Simulate payment** (test mode), or pay by GCash. Then check packing photos, book a Lalamove sandbox delivery, ask the customer to tap **Received**, and open Bea's damage report from her chat, with her photo and unboxing video, then review it.
- To try GCash, press **Send GCash QR code** (or **Already paid by GCash (outside the chat)** for Mark). The customer answers with a made-up receipt, stamped SAMPLE, made in code (the AI never draws an image). Mark's first receipt is always paid to someone else; tell him and he sends one that matches. To confirm a payment the way a seller would, use **Sample history screenshot** in the order, or download the sample GCash history PDF in Settings (password `delacruz4567`) and upload it.
- **Sample GCash history PDF** (Settings): GCash locks its history exports with a password, and so does the sample. Its password is `delacruz4567`: GCash's rule, the account holder's last name and the last 4 digits of their number, for the demo's made-up account (Juan Dela Cruz, 0917 123 4567). Upload it under Settings to confirm the GCash payments of the matching sample receipts you sent.
- **New demo chat** pastes any chat in `Customer:` / `Seller:` lines, or leave it empty and the AI customer starts it by asking about something from the catalog. The seeded customers' personalities stay in their own chats; a new chat gets a polite generic customer. **Reset demo chats** starts over.
- Demo visitors only see their own demo chats and orders, with a made-up GCash account and pickup address. Messenger is live but limited to app testers while the Meta app is in development mode, so real chats are not part of the demo.

## The problem

A small Philippine online seller keeps the order in a Messenger thread, the payment in a GCash screenshot, the packing in their memory and the delivery in a courier app. Nothing ties these together. So the seller can't tell whether a screenshot is a real payment, what was actually agreed after the customer changed their mind, or whether a "missing item" complaint is true. Mistakes cost them goods, wrong refunds and disputes with honest customers.

## What it does

| Stage | What happens |
|---|---|
| **Chat to order** | Gemini reads the Messenger thread and drafts the order: items, quantities, recipient, address. Every field cites the message it came from, and the seller reviews and edits before anything is sent. For a returning customer, the new order is drafted only from the messages after the last order. Name, phone and address the customer doesn't repeat are copied from their last confirmed order and marked as copied, not cited. |
| **Catalog** | The seller's products, each variant with a price, stock and an optional reference photo, in Settings. Drafting matches each item to a catalog line (Gemini picks the line; code looks up every value). A price no seller message states comes from the catalog and says so; a chat price that differs from it, and stock that runs short, are noted on the order; the chat price the customer agreed to stands. The packing check sends each line's reference photo, so a variant named "Sky" can be checked by how it looks. |
| **About this customer** | Above the order: a short paragraph Gemini writes about the customer's history with the shop, from their finished orders (counted in code: outcomes, claims with their recorded causes, refunds, returns, failed deliveries) and the messages about them. The conversation going on now is left to the checks, so a new message never rewrites it; a newly finished order does. Briefs are kept by the hash of their input and shared by every chat with the same history. A first-time customer gets a fixed line reminding the seller to check their Facebook profile, with no Gemini call. It never judges the customer. |
| **Checks before shipping** | Gemini flags specific things in the chat to verify, such as "says they already paid", pressure to ship before payment, asking the seller to open an account, use a link or share a code to "receive" payment, or threatening to post about the shop or report its page. Each check quotes the message. It is never a score or a label on the customer. |
| **Video evidence** | The seller can film packing and sealing; the customer can send an unboxing video in the chat. Videos are stored with their hash and given to Gemini as links to the evidence bucket, so their size never limits a review. The seller prints a packing label with the order code and sticks it on the box; the customer is asked to film the label before opening. Each video gets its own check first (one continuous shot, sealed at the start, the item's condition, the label's order code and name, which code compares with the order), and a label from another order or a cut means the video cannot show the item came out of that parcel: never a sign of dishonesty, a reason to ask for the uncut video. The demo's videos are realistic samples made with Veo. |
| **Customer confirmation** | Everything happens in the chat. The seller sends the order summary with a **Confirm** quick-reply button whose text names the order and version, and code matches the customer's tap exactly; no AI reads it. The payment link (PayMongo QR Ph) goes in the chat too, or the customer pays GCash directly: one click sends the shop's GCash name, number and amount, then the shop's own QR screenshot saved in Settings (the demo shop sends a stamped sample QR that pays no one). Once it is on its way, a **Received** button records receipt, kept apart from the courier's "delivered". A complaint in the chat becomes a claim when the seller picks the messages: the claim keeps the customer's own words and photos. With the Messenger webhook set up, a tap applies the moment it happens: the button carries the order (and version), and it counts only from the customer it was sent to, checked against Meta's signature. Messenger allows replies for 24 hours after the customer's last message (7 days for a person replying, once the app has Meta's Human Agent permission); a fallback for a closed window is on the roadmap. |
| **Payment** | A PayMongo webhook with a verified signature is the only thing that marks a payment as verified. For direct GCash, a customer's screenshot is transcribed by Gemini and checked in code (recipient name and number, amount, date, reference format). It is still only a claim until the seller finds it in their own GCash history, either by screenshot or by uploading the GCash history PDF, which is parsed in code with running-balance checks. If the customer says they paid before being asked, receipts count from the start of the conversation about the order instead of from the request. |
| **Packing** | The seller photographs the packed items. Gemini reports what it can see per order line; code compares that with the confirmed order, and anything uncertain waits for the seller. |
| **Dispatch and delivery** | Booking through Lalamove (sandbox), with status from its signed webhook, or a seller-reported courier. Every delivery event is labelled with its source, and "delivered" (the courier) is kept apart from "received" (the customer). The driver is kept with where it came from: Lalamove (name and masked plate, fetched while the order is active, because Lalamove stops serving them after), simulated (the sandbox, which rarely assigns a driver), or named by the seller (a Favourite Driver or a rider arranged outside Lalamove). Lalamove's pickup and drop-off photos are copied into the evidence bucket. A failed or cancelled shipment records who and why, since Lalamove gives no reason. |
| **Claims** | The customer reports a problem with photos. An ADK agent starts from the claim and the customer's photos, fetches the order's records with read-only tools (the order and payment, the chat, packing with its photos, delivery events), recalls how the shop decided similar claims from Memory Bank, and recommends an outcome, citing evidence IDs. None of its tools can change anything. Its after-agent callback runs code that drops any citation to a record it was not given or did not fetch, and holds back findings left without one. |
| **Custody check and cause** | Each claim's evidence chain (packed, picked up, in transit, dropped off, opened, returned) is read in code on every load. The courier's pickup and drop-off photos each get a narrow Gemini check (the parcel's outside, its seal, its label, which code compares). The first step where the parcel looks wrong suggests a cause: the seller's error, the courier in transit, the customer, or unknown. The seller records the cause with the decision. A missing photo or video never counts against the courier or the customer. |
| **Refunds, replacements and returns** | Separate, explicit seller actions after a decision: a PayMongo API refund, a recorded manual refund, or a replacement shipment. The seller can ask for the item back: the customer sends it with a courier of their choice and the seller records the courier and tracking number they give, then marks it received, photographs it, checks it against the order and records what came back. The seller books nothing. A refund on that claim waits, in code, until the item is received. Old records are kept, never overwritten. |
| **Patterns** | Store-wide cards. **Rising this week** compares the last 7 days with the 3 weeks before: receipts that don't fit the order or the shop's GCash, requests to open an account or share a code (from the chat checks), and damage reports. Below are concentrations: a payment reference reused across orders, repeated claims, damage concentrated on a courier, vehicle, area, driver or product, and missing or replaced contents on one driver. Only claims recorded as transit or unknown count against a courier or driver. Returns by cause shows counts, rates and cost. Each card shows its counts, the rest of the store for comparison, and its limitations. Cards never block anything. In the demo, seven chats from the past week feed these cards, and most chats come with earlier orders from the last few weeks, delivered by simulated sandbox riders, some with a claim decided and refunded, so Orders and Patterns have history to show. |

## Design rules

- **Gemini transcribes and cites; code decides.** Every pass/fail result, gate and money decision is made in Python. Nothing runs because the AI recommended it.
- **Only the seller sets a price.** An extracted price is kept only if a seller message actually states that number. Otherwise it is left blank for the seller to type.
- **Chat text is data, not instructions.** Customer, courier and model-written text is flattened to one line before it goes into a prompt, so it can't pose as a seller message or a payment record. Prompt injection has a unit test and live Gemini eval cases (fake seller lines, a `</chat>` breakout, "ignore instructions", a fake payment record, a Tagalog instruction, and a fake record in a claim).
- **Records are appended.** Replaced shipments, re-packing and refunds keep their history.

## How it uses Google Cloud

| Service | Use |
|---|---|
| **Gemini** (via Vertex AI on Agent Platform) | Eight structured-output calls: chat-to-order extraction (matched to the shop's catalog), checks before shipping, the customer brief, packing photo review, GCash receipt transcription, GCash history screenshot transcription, the claim evidence review, and, in the demo only, the customer (on Gemini Flash-Lite, since it only writes chat lines): it writes each demo customer's replies in character, while code decides when they tap a button and which sample receipt they send. |
| **Agent Development Kit (ADK)** | The claim review runs as an ADK `LlmAgent` with five read-only function tools (`read_order`, `read_chat`, `read_packing`, `read_delivery`; packing photos come back as images in the tool result; and `read_past_decisions`, from Memory Bank) and a structured output schema, through an ADK `Runner` with one in-memory session per review. The citation check runs as the agent's after-agent callback, against only what the agent was given or fetched. |
| **Memory Bank** (Agent Platform) | The shop's past claim decisions. When the seller approves an outcome, the backend writes one memory as given, with no AI rewriting: the claim type, the items and the seller's decision and reason, never the customer's words, so nothing a customer writes can be stored and replayed into later reviews. The claim reviewer recalls the closest past decisions by similarity search, scoped to the store (or to one demo visitor, plus shared demo examples), and cites them as P1, P2… The prompt and an eval case keep them as practice, never as evidence about this customer. If Memory Bank is off or down, reviews go on without it. |
| **Model Armor** | A second screen beside the prompts' own defences: each customer message and each GCash receipt screenshot is checked for prompt injection, and messages for unsafe links, at the same time as the Gemini call, so it adds no wait. It only flags: a finding becomes a check before shipping or a receipt check that does not fit, never a block. Off, or not answering, means not screened, never shown as a pass. The template is in the `us` multi-region, since image screening (Preview) runs only in us and eu. |
| **Gemini image generation** | The demo's product photos (catalog references, packing and damage photos) were made once with `gemini-3.1-flash-image`, each packing or damage photo from its product's reference so it shows the same item, and stamped as AI-generated samples (`backend/seed/make_images.py`). A real seller's catalog uses photos of their own products. |
| **Google Maps Platform** | The Geocoding API suggests the drop-off and pickup pins from the address, and the Maps JavaScript API shows them on a map to drag into place. Lalamove needs a pin and does not geocode. Philippine addresses often geocode to the wrong street or city (even at Google's top precision), so the backend compares the city and street with the address and tells the seller in plain words what to check. A Google Maps link, including a short share link the customer sends in the chat, is read for its pin, following redirects only through Google's hosts. Keys: the server key may only geocode; the browser key only loads maps, only on this app's addresses. |
| **Veo video generation** | The demo's and the eval's unboxing and packing videos were made once with `veo-3.1-generate-001` from the catalog's photo of the blue mug, so it is the same mug, and every frame is stamped "SAMPLE VIDEO · AI GENERATED" (`backend/seed/make_videos.py`). |
| **Cloud Run** | The FastAPI backend: all API routes, the Messenger Graph API client, and the Messenger, PayMongo and Lalamove webhooks. Scales to zero. |
| **Firebase Hosting** | Serves the React frontend and routes `/api/**` to Cloud Run on the same domain. |
| **Firebase Authentication** | Google sign-in for sellers, checked against an email allowlist, and anonymous sign-in for demo visitors. The backend verifies the ID token on every protected route and limits demo visitors to their own demo chats and orders. |
| **Firestore** | Orders with their full history, claims, refunds, delivery events, settings and webhook dedupe records. Security rules deny all browser access; everything goes through the backend. |
| **Cloud Storage** | Evidence files: packing and claim photos, GCash screenshots, images sent in replies. Stored by content hash. |
| **Secret Manager** | API keys (Messenger, PayMongo, Lalamove) reach Cloud Run as secrets, readable only by the service account. |

Other services: Messenger Graph API and webhook (conversations, replies and quick-reply taps), PayMongo (checkout, webhooks, refunds, in test mode), Lalamove (sandbox quotes, bookings and webhooks).

## Repository

```
backend/    FastAPI on Cloud Run
  main.py           the app: includes one router per area
  routes/           HTTP routes by area: conversations, demo, orders, customer (the public customer API; its page is not in the demo UI), gcash, packing,
                    dispatch, claims, refunds, patterns, webhooks
  core/             config, auth (sign-in, allowlist, demo gate, demo AI limits) and Firestore/Storage access
  integrations/     Messenger Graph API, PayMongo and Lalamove clients
  services/         shared helpers the routes call: demo chats, demo seeding, the customer link, payment and delivery sync
  domain/           business rules as pure functions, no I/O:
    extraction.py     chat to order, price check, prompt-text flattening
    signals.py        checks before shipping, and which order each check belongs to
    orders.py         order versions, confirmation and payment rules
    receipts.py       GCash receipt, history screenshot and history PDF checks
    packing.py        packing photo checks, including the blur checks (code's sharpness score, and Gemini's list of unclear photos)
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
  src/customer/       the page a customer sees after paying with PayMongo
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

Extraction works on a normalized message list; Messenger is the first adapter. Next: Instagram DMs (same Meta app and Graph API), then Facebook and Instagram comments (moving "mine" and price questions into DMs), then WhatsApp. A way to finish an order once Messenger's 24-hour reply window has closed: a customer page (its API is kept in routes/customer.py) or Meta's Human Agent tag.

## Team

- **Kevin Gil Soriano**, Lead Full Stack Engineer
- **Jan Beatrix Escarda**, Full Stack Engineer
