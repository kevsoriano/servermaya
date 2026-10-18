# Recording the demo video flows

Four scripted flows for the demo video (up to 4 minutes), recorded with Playwright as a new demo visitor. Each one ends on
the **Patterns** tab, showing a pattern the seller's own action just added to. Each run gives an MP4 that starts on the
loaded chat list (sign-in is cut), one screenshot per step and a timing file.

Each video starts on the chat list and goes straight into its chat; Patterns appears only at the end. Before the cut, the
recorder opens Patterns once, unseen: the tab remembers each card's count in the browser, so the last step's card still
shows **Up from 3 since your last look** or **Appeared since your last look**.

Warm up before recording (the warm-up section below): the first session after the servers start compiles the frontend and
opens the backend's connections, and costs a take 10–15 s.

The script only acts as the seller. Demo customers are played by AI and answer on their own after each seller message
(Mark in A, Dan in B), with a "typing" indicator while Gemini writes. Code, not the AI, decides when they tap a button and
which sample receipt they send, so Mark always taps Confirm, then sends a receipt paid to someone else. Dan's threat is his
persona's words, so it can fail to come in a take: re-record.

| Flow | Chat | What it shows | On Patterns | Last length |
|---|---|---|---|---|
| A | Mark | The brief and the checks alert, a draft order, Mark taps Confirm in chat, "Already paid by GCash" and Mark answers with a receipt paid to someone else; the receipt check shows the recipient does not fit | **Count goes up**: "Receipts that don't fit the order or your GCash account" from 3 to 4 (Mark, Paolo, Grace, Jerome), each with a push to ship first | 1:19 |
| B | Dan | A new customer, Binance and a code request, the checks; the seller says GCash only and Dan threatens to post about the shop in Facebook groups and report the page, which the checks add | **New card**: "Asked you to open an account or share a code", 3 chats (Dan, Kim's Maya code, Ramon's OTP link) against 0 before | 0:55 |
| C | Bea | Her photo and unboxing video of a cracked mug; the seller opens a claim from her messages, then Review with AI (the packing video shows it whole, her video shows it sealed, then cracked) | **New card**: "Damage reports", 3 this week (Bea, Nico, Lito) against 1 a week before; below it, Sample Rider 2 carried most of them | 1:02 |
| D | Carla | Her box arrived with only crumpled paper inside; the seller opens a Missing item claim from her messages, with the custody check beside it | **Count goes up**: "Missing or replaced contents" on Sample Rider 3, from 2 to 3 parcels (Mark, Rico, Carla), against 0 of 13 for other drivers | 0:34 |

Lengths are untrimmed, with pauses standing in for narration (halved in A). Gemini waits (the draft, the claim review) and
the Patterns tab loading (2–3 s) are worth trimming. Together the four flows are about 3:50 untrimmed. A's receipt check took 33 s in its take, against 5–6 s on a good day, while
Vertex was slow reading images: the viewer only waits on "Not checked yet" meanwhile, so cut it down; if they don't fit
with an intro, drop C or D first, since both end on a driver card.

The cards never say a customer did anything wrong. Narrate what they show: receipts that don't fit, requests to open an
account, damage reports, parcels that arrived without their contents.

## What the seed sets up

Every demo visitor gets 15 chats: the 8 to work through, and 7 from the past week behind the Patterns cards. Their checks
are stored when the demo is seeded, so the counts are the same on every run. The chats and their checks are defined in
[backend/seed/demo.py](../backend/seed/demo.py).

- **Receipts (A):** Paolo (paid to someone else), Grace (₱100 short) and Jerome (dated before the GCash request), each asking
  to ship first. 3 this week, so the card is already there.
- **Accounts (B):** Kim (open a Maya account, send the 6-digit code) and Ramon (log in through a link, share the OTP). 2 is
  below the minimum of 3, so no card until Dan's chat is opened.
- **Damage (C):** Nico's and Lito's broken mugs this week, both carried by Sample Rider 2. Mark's, Leah's and Bea's earlier
  reports, 2 to 4 weeks ago, are the comparison. Bea's report waits in her chat, with no claim open yet.
- **Contents (D):** Mark's missing candle and Rico's replaced tote, earlier this month, both carried by Sample Rider 3.
  Carla's parcel was carried by Sample Rider 3 too. Joy's wrong tote was recorded as the shop's own error and never counts
  against a driver.

## Prompt for a new session

Paste this into Claude Code from the repo root:

> Re-record the demo video flows A, B, C and D by following docs/demo-video.md. Set up the recorder in your scratchpad, run
> the backend without --reload and the frontend dev server, run warmup.mjs once, record the flows one after the other, and check the screenshots
> of the key steps (listed in the doc) before reporting. If a step fails because the UI changed, update the script's
> selectors or waits rather than the app, and tell me what you changed. Report each video's path and length, and stop the
> servers you started when done.

## Setup (in a scratch folder, not the repo)

```sh
mkdir rec && cd rec
echo '{"type":"module"}' > package.json
npm install playwright@1.64.1 ffmpeg-static@5.3.0
npx playwright install chromium
# then save the two scripts at the end of this doc as record.mjs and warmup.mjs
```

## Run

The local app must be running. **Start the backend without `--reload`**: `make dev`'s file watcher polls the ~4,200 `.py`
files under `backend/` (most in `.venv`) and made every request about 5× slower (a message fetch 910 ms instead of
170–350 ms), which stretched an earlier Flow A from 1:09 to 2:03.

```sh
# terminal 1, from the repo
cd backend && uv run --env-file .env uvicorn main:app --port 8000
# terminal 2
cd frontend && npm run dev          # http://localhost:5173 (the Firebase key allows only this origin locally)
# terminal 3, in the rec folder: the app only lets in a session that enters the access code
export ACCESS_CODE=$(grep '^ACCESS_CODE=' ../ai-builder-cup/backend/.env | cut -d= -f2-)   # adjust the path to the repo
# warm up once, then record one after the other, not in parallel
node warmup.mjs          # the script below record.mjs: compiles the frontend, connects the backend, caches the briefs
node record.mjs A out-A
node record.mjs B out-B
node record.mjs C out-C
node record.mjs D out-D
```

`APP_URL=https://servermaya-ai-builder-cup.web.app node record.mjs A out-A` records the live app instead, once it is
deployed with the same UI.

Output in each folder: `flow-A.mp4` (trimmed), `flow-A.webm` (untrimmed), `A-01.png`… (one per step, taken when the
step's action finishes) and `flow-A-timing.json` (step start times in the trimmed video's time, for editing).

## Check before using a take

The script prints each step's time and `FAILED` on a failure; a failed step still continues, so read the log. Then look
at these screenshots:

- **A-01**: Mark's brief and the orange "2 to verify before shipping" alert.
- **A-04**: Mark's own "Confirm #… v1" under the summary, and the Confirmed row "By the customer".
- **A-05**: Mark's words (in character, such as "Here is the screenshot. Please ship it today!") and his receipt below them.
- **A-07**: the viewer with Recipient name ✗ and Recipient number ✗ (MA\*\*A S\*\*\*\*S, +63 918 555 \*\*\*\*), Amount ✓, and
  Model Armor's "No text aimed at the AI" ✓.
- **A-09**: "Receipts that don't fit…" with **Up from 3 since your last look**, 4 chats, Mark Dizon listed "today".
- **B-03**: Dan's card "Asked you to open an account, use a link or share a code" with its What to do.
- **B-04**: the seller's "we only accept GCash" and Dan's answer below it, a threat to post about the shop or report the
  page (in character, such as "Otherwise I'll report this page and post about your shop in Facebook groups").
- **B-05**: Dan's checks with "Threatened to post about you or report your page", quoting him, and its What to do.
- **B-06**: "Asked you to open an account or share a code" with **Appeared since your last look**: Dan today, Kim, Ramon.
- **C-01**: Bea's chat: her photo and unboxing video.
- **C-03**: the new Damaged claim with her words, photo and video.
- **C-04**: the AI review's findings (for example "Supported: … damaged …") and its suggested action.
- **C-05**: "Damage reports" with **Appeared since your last look**, 3 reports: Bea today, Nico, Lito. Bars 2 / 1 / 0 / 3.
- **C-06**: "Damage and non-receipt concentration · Driver Sample Rider 2", higher than the rest of the store.
- **D-01**: Carla's chat: the open box with crumpled paper.
- **D-03**: the Missing item claim, her words and photo, "Custody check: … suggests Unknown".
- **D-04**: "Missing or replaced contents · Driver Sample Rider 3" with **Up from 2 since your last look**, 3 of 6: Mark,
  Carla, Rico.

Each run is a new demo visitor, so it starts from the seeded state. Gemini times vary: the same step can take 4 s or 60 s
(the draft order took 57 s once). Mark's and Dan's words change from take to take; Mark's taps and receipts do not.
Re-record or trim long waits in editing.

## When something goes wrong

- **A step times out after a UI change**: the selectors the script relies on are `.threads button`, `.brief .brief-text`,
  `.checks-alert`, `section.signals` and its `.pill` and `li.to-verify`, `.verify-head`, `.gates li`, `.chat .media-open`,
  `.messages li.typing` (the customer is answering), `dialog.viewer[open]` with its `h2` and `.viewer-close`, `form.reply`,
  `nav.tabs button`, `.signal.pattern` (a Patterns card), `section.packing` with its `select`, `form` and
  `div.claim:not(.done)` (the claims on an order), the `/customer/auto`, `/claims/from-chat` and `/claims/…/review`
  requests, the access-code form (`main.card input` and its "Enter" button), and button texts ( "Create draft order", "Send for confirmation in chat", "Already paid by
  GCash", "Check receipt screenshots", "Open order →", "Open claim from the chat", "Open claim", "Review with AI"). Update
  the script to match.
- **A card is missing or its count is off by one**: the Patterns tab was opened before the seller's action was saved, or
  the seed changed. The counts come from `backend/seed/demo.py` (`MISFITS`, `ACCOUNTS`, `HISTORY`); the minimum for a rising
  card is `TREND_MIN` in `backend/domain/patterns.py`.
- **"Mark Dizon could not reply right now." with a Try again button** (or Dan): Gemini failed twice in a row on the reply
  (the route retries once). The step times out; record again. A receipt Mark owes right after a GCash request is sent even
  when Gemini fails, just without words.
- **A customer step takes 30 s or more**: Vertex is rate-limiting (a `429 RESOURCE_EXHAUSTED` in the backend log), usually
  after several takes in a row. Wait a few minutes and record again.
- **"Could not read a screenshot. Try again."** at Check receipt screenshots: Gemini failed twice in a row on the read (it
  retries once, with a "screenshot read attempt" warning in the backend log). Record again.
- **The access code is refused**: `ACCESS_CODE` is not set in the shell, or differs from the one the backend runs with.
- **The chat list stays on "Loading…" after the access code**: the local backend got stuck, seen once after a failed run.
  Restart it and record again.
- **The video opens on a click instead of the still list**: the cut landed late. Raise the 1500 ms hold after the cut.

## record.mjs

```js
// Records one demo flow, as a new demo visitor (APP_URL, default the local dev server), and logs how long each step takes.
// Usage: node record.mjs <A|B|C|D> <out dir>
import { chromium } from 'playwright'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import ffmpeg from 'ffmpeg-static'

const URL = process.env.APP_URL ?? 'http://localhost:5173'
const CODE = process.env.ACCESS_CODE
if (!CODE) throw new Error('Set ACCESS_CODE: the app only lets in sessions that enter it')
const [flow, out] = process.argv.slice(2)
fs.mkdirSync(out, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, recordVideo: { dir: out, size: { width: 1440, height: 900 } } })
// A ring where each click lands, since the recording has no pointer.
await ctx.addInitScript(() => {
  document.addEventListener(
    'click',
    (e) => {
      const d = document.createElement('div')
      Object.assign(d.style, {
        position: 'fixed', left: `${e.clientX - 18}px`, top: `${e.clientY - 18}px`, width: '36px', height: '36px',
        borderRadius: '50%', border: '3px solid #ff3b30', background: 'rgba(255,59,48,0.15)', zIndex: 99999,
        pointerEvents: 'none', transition: 'transform .5s, opacity .5s',
      })
      document.body.appendChild(d)
      requestAnimationFrame(() => Object.assign(d.style, { transform: 'scale(1.6)', opacity: '0' }))
      setTimeout(() => d.remove(), 700)
    },
    true,
  )
})
const page = await ctx.newPage()
// Flow A is narrated over the action, so its pauses are half as long.
const SCALE = flow === 'A' ? 0.5 : 1
const t0 = Date.now()
const log = []
let n = 0

// One step: the action and its waits (timed), a screenshot, then a pause standing in for narration.
async function step(name, fn, pause = 2500) {
  pause = Math.round(pause * SCALE)
  const s = Date.now()
  let ok = true
  try {
    await fn()
  } catch (e) {
    ok = false
    console.error(`step "${name}" failed: ${e.message.split('\n')[0]}`)
  }
  const took = (Date.now() - s) / 1000
  n += 1
  await page.screenshot({ path: `${out}/${flow}-${String(n).padStart(2, '0')}.png` })
  await page.waitForTimeout(pause)
  log.push({ step: n, name, start: (s - t0) / 1000, action: took, pause: pause / 1000, ok })
  console.log(`${String(n).padStart(2)}  ${name.padEnd(58)} ${took.toFixed(1).padStart(5)}s ${ok ? '' : 'FAILED'}`)
}

const click = (sel) => page.locator(sel).first().click()
const show = (sel) => page.locator(sel).first().scrollIntoViewIfNeeded()
const pane = () => page.locator('aside.pane.side')
const newOrder = 'button:has-text("Create draft order"), button:has-text("New order from the newer messages")'
const chat = (name) => page.locator(`.threads button:has-text("${name}")`).first()

// Each demo customer is played by AI and answers on their own after each seller message. Wait for the answer to land: the
// request that writes it, then the typing dots gone, and, when they send a receipt, one more screenshot in the chat.
const customerAnswers = async (action, receipt = false) => {
  const shots = await page.locator('.chat .media-open').count()
  const done = page.waitForResponse((r) => r.url().includes('/customer/auto') && r.request().method() === 'POST', { timeout: 120_000 })
  await action()
  await done
  await page.locator('.messages li.typing').waitFor({ state: 'detached', timeout: 30_000 })
  if (receipt) await page.waitForFunction((n) => document.querySelectorAll('.chat .media-open').length > n, shots, { timeout: 30_000 })
}
// The seller types a message, as a person would, and sends it; the customer answers.
const sellerSays = async (text, receipt = false) => {
  const form = page.locator('form.reply').first()
  await form.locator('textarea').click()
  await form.locator('textarea').pressSequentially(text, { delay: 18 })
  await customerAnswers(() => form.locator('button:has-text("Send")').click(), receipt)
}

// Signing in is not part of the video: it is cut from the start at the moment the chats have loaded.
await page.goto(URL)
// The one way in: the access code (ACCESS_CODE), as a judge enters it.
await page.locator('main.card input').fill(CODE)
await click('main.card button:has-text("Enter")')
await chat('Mark Dizon').waitFor({ timeout: 120_000 })
await page.waitForTimeout(500) // the list's previews and avatars settle
// Patterns, unseen: one visit before the cut, so each flow's last step can show what its action changed ("Up from 3 since
// your last look", "Appeared since your last look") without spending video time on the cards as they were.
await click('nav.tabs button:has-text("Patterns")')
await page.locator('.signal.pattern').first().waitFor({ timeout: 90_000 })
await click('nav.tabs button:has-text("Conversations")')
await chat('Mark Dizon').waitFor({ timeout: 60_000 })
await page.waitForTimeout(500)
const cut = (Date.now() - t0) / 1000
console.log(`video starts at ${cut.toFixed(1)}s (signed in, chats loaded)`)
// The video's clock starts a little after the page opens, so the cut lands slightly late: hold the loaded list on screen first.
await page.waitForTimeout(1500)

// Patterns at the end of each flow: the card the seller's action changed, marked against the unseen visit before the cut.
const patternsTab = async () => {
  await click('nav.tabs button:has-text("Patterns")')
  await page.locator('.signal.pattern').first().waitFor({ timeout: 90_000 })
}
const showCard = async (text) => {
  const card = page.locator('.signal.pattern').filter({ hasText: text }).first()
  await card.waitFor({ timeout: 30_000 })
  await card.evaluate((el) => el.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  await page.waitForTimeout(800)
}
const openBrief = async (name) => {
  await chat(name).click()
  await page.locator('.brief .brief-text p:not(.muted)').waitFor({ timeout: 90_000 })
}
// From the chat's order to its claims in the Orders tab, and a claim opened from the customer's own messages.
const claimFromChat = async (type) => {
  await click('button:has-text("Open order →")')
  await click('button:has-text("Open claim from the chat")')
  await page.locator('section.packing select').first().selectOption(type)
  const form = page.locator('section.packing form').filter({ hasText: 'What the customer reports' }).first()
  await form.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' }))
  await page.waitForTimeout(800)
}
const showClaim = async () => {
  const claim = page.locator('section.packing div.claim:not(.done)').first()
  await claim.waitFor({ timeout: 30_000 })
  await claim.evaluate((el) => el.scrollIntoView({ block: 'start' }))
  await page.waitForTimeout(600)
}
const openClaim = async () => {
  const saved = page.waitForResponse((r) => r.url().includes('/claims/from-chat') && r.request().method() === 'POST', { timeout: 60_000 })
  await click('section.packing button:has-text("Open claim")')
  await saved
  await showClaim()
}

if (flow === 'A') {
  await step('Open Mark: AI brief, and an alert: 2 to verify', async () => {
    await openBrief('Mark Dizon')
    await page.locator('.checks-alert').waitFor({ timeout: 90_000 })
  }, 4000)
  await step('Click the alert: what was said, what to do, the records', async () => {
    await click('.checks-alert')
    await page.waitForTimeout(800)
  }, 5000)
  await step('Create draft order (cited to his messages)', async () => {
    await pane().evaluate((el) => el.scrollTo(0, 0))
    await click(newOrder)
    await page.locator('.verify-head:has-text("Draft")').waitFor({ timeout: 90_000 })
  }, 3000)
  await step('Send for confirmation in chat: Mark taps Confirm', async () => {
    await customerAnswers(() => click('button:has-text("Send for confirmation in chat")'))
    await page.locator('.gates li:has-text("By the customer")').waitFor({ timeout: 30_000 })
  })
  await step('Already paid by GCash: Mark sends a receipt paid to someone else', () =>
    customerAnswers(() => click('button:has-text("Already paid by GCash")'), true),
  )
  await step('Open his screenshot in the chat: not checked yet', async () => {
    await page.locator('.chat .media-open').last().click()
    await page.locator('dialog.viewer[open] h2:has-text("Not checked yet")').waitFor({ timeout: 15_000 })
  }, 2500)
  await step('Check receipt screenshots: the recipient does not fit', async () => {
    await click('dialog.viewer[open] button:has-text("Check receipt screenshots")')
    await page.waitForFunction(() => /Pass|Does not fit|fail/i.test(document.querySelector('dialog.viewer[open]')?.textContent ?? ''), null, { timeout: 120_000 })
  }, 6000)
  await step('Close', () => click('dialog.viewer[open] .viewer-close'), 800)
  await step('Patterns: up from 3 to 4, Mark listed today', async () => {
    await patternsTab()
    await showCard('Up from 3')
  }, 7000)
} else if (flow === 'B') {
  await step('Open Dan: AI brief (new customer, Binance, code)', async () => {
    await openBrief('Dan Cortez')
  }, 4000)
  await step('Click the alert: his checks before shipping', async () => {
    await page.locator('.checks-alert').waitFor({ timeout: 90_000 })
    await click('.checks-alert')
    await page.waitForTimeout(800)
  }, 3000)
  await step('The code request: what to do', async () => {
    const card = page.locator('section.signals li.to-verify').filter({ hasText: 'Asked you to open an account' }).first()
    await card.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' }))
    await page.waitForTimeout(800)
  }, 5000)
  await step('The seller says GCash only; Dan, played by AI, threatens to post about the shop', async () => {
    await sellerSays("Sorry Dan, we only accept GCash. We can't open other accounts or share any codes.")
    await page.locator('.chat .scroll').evaluate((el) => el.scrollTo(0, el.scrollHeight))
  }, 4000)
  await step('The checks add it: threatened to post about you or report your page', async () => {
    const card = page.locator('section.signals li.to-verify').filter({ hasText: 'Threatened to post' }).first()
    await card.waitFor({ timeout: 90_000 })
    await card.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' }))
    await page.waitForTimeout(800)
  }, 5000)
  await step('Patterns: a new card, 3 chats asked for an account or a code', async () => {
    await patternsTab()
    await showCard('Asked you to open an account')
  }, 7000)
} else if (flow === 'C') {
  await step('Open Bea: her photo and unboxing video of a cracked mug', async () => {
    await openBrief('Bea Villanueva')
    await page.locator('.chat .scroll').evaluate((el) => el.scrollTo(0, el.scrollHeight))
  }, 4000)
  await step('Open her order, then Open claim from the chat: Damaged', () => claimFromChat('damage'), 3000)
  await step('Open claim: her words, photo and video become the claim', openClaim, 3000)
  await step('Review with AI: the packing video, her unboxing video, the custody check', async () => {
    const reviewed = page.waitForResponse((r) => /\/claims\/[^/]+\/review$/.test(r.url()) && r.request().method() === 'POST', { timeout: 180_000 })
    await click('section.packing div.claim:not(.done) button:has-text("Review with AI")')
    await reviewed
    // The findings, not the top of the claim: her photo and video above them fill the screen.
    const review = page.locator('section.packing div.claim:not(.done) .review').first()
    await review.waitFor({ timeout: 30_000 })
    await review.evaluate((el) => el.scrollIntoView({ block: 'start' }))
    await page.waitForTimeout(600)
  }, 7000)
  await step('Patterns: a new card, 3 damage reports this week', async () => {
    await patternsTab()
    await showCard('Damage reports')
  }, 6000)
  await step('Below: the same rider carried most of them', () => showCard('Driver Sample Rider 2'), 5000)
} else {
  await step('Open Carla: the box arrived with only paper inside', async () => {
    await openBrief('Carla Villamor')
    await page.locator('.chat .scroll').evaluate((el) => el.scrollTo(0, el.scrollHeight))
  }, 4000)
  await step('Open her order, then Open claim from the chat: Missing item', () => claimFromChat('missing_item'), 3000)
  await step('Open claim: the custody check beside it', openClaim, 5000)
  await step('Patterns: Sample Rider 3 up from 2 to 3', async () => {
    await patternsTab()
    await showCard('Up from 2')
  }, 7000)
}

await page.waitForTimeout(1000)
const video = page.video()
await ctx.close()
await browser.close()
const webm = await video.path()
fs.renameSync(webm, `${out}/flow-${flow}.webm`)
// The kept video, from the cut; step times below are in the kept video's time.
execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-ss', cut.toFixed(2), '-i', `${out}/flow-${flow}.webm`, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20', '-movflags', '+faststart', `${out}/flow-${flow}.mp4`])
const total = (Date.now() - t0) / 1000 - cut
const steps = log.map((s) => ({ ...s, start: +(s.start - cut).toFixed(1) }))
fs.writeFileSync(`${out}/flow-${flow}-timing.json`, JSON.stringify({ total, cut, steps }, null, 2))
console.log(`total ${total.toFixed(1)}s after the cut`)
```

## warmup.mjs

Run once after starting the servers, before the takes. Each line prints ok or FAIL with the time; fix a FAIL before
recording.

```js
// A throwaway demo session before the takes: compiles the frontend, opens the backend's Firestore and Gemini connections,
// and writes the briefs of the four flows' customers (briefs are shared by every visitor with the same chat history).
import { chromium } from 'playwright'
const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
p.on('console', (m) => m.type() === 'error' && console.log('CONSOLE', m.text().slice(0, 160)))
const s = Date.now(), t = () => `${((Date.now() - s) / 1000).toFixed(1)}s`
const ok = async (label, fn) => {
  try { await fn(); console.log(`ok   ${label.padEnd(30)} ${t()}`) } catch (e) { console.log(`FAIL ${label.padEnd(30)} ${t()} ${e.message.split('\n')[0]}`) }
}
await p.goto('http://localhost:5173')
await ok('access code and seed', async () => {
  await p.locator('main.card input').fill(process.env.ACCESS_CODE ?? '')
  await p.locator('main.card button:has-text("Enter")').click()
  await p.locator('.threads button:has-text("Mark Dizon")').first().waitFor({ timeout: 120000 })
})
for (const name of ['Mark Dizon', 'Dan Cortez', 'Bea Villanueva', 'Carla Villamor'])
  await ok(`brief: ${name}`, async () => {
    await p.locator(`.threads button:has-text("${name}")`).first().click()
    await p.locator('.brief .brief-text p:not(.muted)').waitFor({ timeout: 120000 })
  })
await ok('checks (Gemini) on Carla', () => p.locator('section.signals').first().waitFor({ timeout: 120000 }))
await ok('Patterns', async () => {
  await p.locator('nav.tabs button:has-text("Patterns")').click()
  await p.locator('.signal.pattern').first().waitFor({ timeout: 90000 })
})
await b.close()
```
