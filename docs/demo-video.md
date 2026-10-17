# Recording the demo video flows

Two scripted flows for the 3-minute demo video, recorded with Playwright as a new demo visitor. Each run gives an MP4
that starts on the loaded chat list (sign-in is cut), one screenshot per step and a timing file.

The script only acts as the seller. Each demo customer is played by AI and answers on their own after every seller
message, with a "typing" indicator while Gemini writes. Code, not the AI, decides when they tap a button and which sample
receipt they send, so Mark always taps Confirm, then sends a receipt paid to someone else, then a matching one once the
seller points it out.

| Flow | Chats | What it shows | Last length |
|---|---|---|---|
| A | Mark | The brief and the checks alert, a draft order, Mark taps Confirm in chat, "Already paid by GCash" and Mark answers with a receipt paid to someone else (recipient ✗), the seller's typed reply and Mark's apology with a matching receipt (passes, still only a claim), the seller finds it in their GCash history, the checks settle to "None open" | 2:05* |
| B | Dan, then Joy | Dan: a new customer, Binance and a code request, the checks cards. Joy: a fake "SELLER:" line and an instruction in her message; the draft keeps the seller's price and her check stays open | 0:42 |

\* The first take with the AI customer. Mark's second reply took 41 s because Vertex was rate-limiting (429) at the time;
on its own that reply takes 2–4 s, so a normal take is about 1:25. Re-record, or cut the wait in editing.

## Prompt for a new session

Paste this into Claude Code from the repo root:

> Re-record the demo video flows A and B by following docs/demo-video.md. Set up the recorder in your scratchpad, run the
> backend without --reload and the frontend dev server, record A then B one after the other, and check the screenshots of
> the key steps (listed in the doc) before reporting. If a step fails because the UI changed, update the script's selectors
> or waits rather than the app, and tell me what you changed. Report each video's path and length, and stop the servers
> you started when done.

## Setup (in a scratch folder, not the repo)

```sh
mkdir rec && cd rec
echo '{"type":"module"}' > package.json
npm install playwright@1.64.1 ffmpeg-static@5.3.0
npx playwright install chromium
# then save the script below as record.mjs
```

## Run

The local app must be running. **Start the backend without `--reload`**: `make dev`'s file watcher polls the ~4,200 `.py`
files under `backend/` (most in `.venv`) and made every request about 5× slower (a message fetch 910 ms instead of
170–350 ms), which stretched Flow A from 1:09 to 2:03.

```sh
# terminal 1, from the repo
cd backend && uv run --env-file .env uvicorn main:app --port 8000
# terminal 2
cd frontend && npm run dev          # http://localhost:5173 (the Firebase key allows only this origin locally)
# terminal 3, in the rec folder: one after the other, not in parallel
node record.mjs A out-A
node record.mjs B out-B
```

`APP_URL=https://servermaya-ai-builder-cup.web.app node record.mjs A out-A` records the live app instead, once it is
deployed with the same UI.

Output in each folder: `flow-A.mp4` (trimmed), `flow-A.webm` (untrimmed), `A-01.png`… (one per step, taken when the
step's action finishes) and `flow-A-timing.json` (step start times in the trimmed video's time, for editing).

## Check before using a take

The script prints each step's time and `FAILED` on a failure; a failed step still continues, so read the log. Then look
at these screenshots:

- **A-01**: the brief and the orange "2 to verify before shipping" alert.
- **A-04**: Mark's own "Confirm #… v1" under the summary, and the Confirmed row "By the customer".
- **A-05**: Mark's words (in character, such as "Here is the screenshot. Please make sure to ship it today!") and his
  receipt below them.
- **A-07**: the viewer with Recipient name ✗ and Recipient number ✗ (MA\*\*A S\*\*\*\*S, +63 918 555 \*\*\*\*), Amount ✓.
- **A-09**: Mark's apology (an old saved number, paid again) and the new receipt.
- **A-11**: the matching receipt, all ✓, still "a claim, not proof of payment".
- **A-15**: Checks before shipping says "None open", three checks settled by the GCash payment the seller found.
- **B-03**: Dan's card "Asked you to open an account, use a link or share a code" with its What to do.
- **B-05**: Joy's draft is 3× Linen pouch at ₱150 (₱450), not ₱100, and the Payment row says the customer says they paid: not verified.
- **B-06**: Joy's checks loaded: "Says they already paid", 1 to verify.

Each run is a new demo visitor, so it starts from the seeded state. Gemini times vary: the same step can take 4 s or 24 s.
Mark's words change from take to take; his taps and receipts do not. Re-record or trim long waits in editing.

## When something goes wrong

- **A step times out after a UI change**: the selectors the script relies on are `.threads button`, `.brief .brief-text`,
  `.checks-alert`, `section.signals` and its `.pill` and `li.to-verify`, `.verify-head`, `.gates li`, `.chat .media-open`,
  `.messages li.typing` (the customer is answering), `dialog.viewer[open]` with its `h2` and `.viewer-close`, `form.reply`,
  the `/customer/auto` request (the customer's answer), and button texts ("Try the demo", "Create draft order", "Send for
  confirmation in chat", "Already paid by GCash", "Check receipt screenshots", "Sample history screenshot", "Confirm GCash
  payment"). Update the script to match.
- **"Mark Dizon could not reply right now." with a Try again button**: Gemini failed twice in a row on his reply (the
  route retries once). The step times out; record again. A receipt Mark owes right after a GCash request is sent even when
  Gemini fails, just without words.
- **A Mark step takes 30 s or more**: Vertex is rate-limiting (a `429 RESOURCE_EXHAUSTED` in the backend log), usually
  after several takes in a row. Wait a few minutes and record again.
- **"Could not read a screenshot. Try again."** at Sample history screenshot: a one-off Gemini failure (that read has no
  retry). Record again.
- **The chat list stays on "Loading…" after Try the demo**: the local backend got stuck, seen once after a failed run.
  Restart it and record again.
- **The video opens on a click instead of the still list**: the cut landed late. Raise the 1500 ms hold after the cut.

## record.mjs

```js
// Records one demo flow, as a new demo visitor (APP_URL, default the local dev server), and logs how long each step takes.
// Usage: node record.mjs <A|B> <out dir>
import { chromium } from 'playwright'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import ffmpeg from 'ffmpeg-static'

const URL = process.env.APP_URL ?? 'http://localhost:5173'
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

// Signing in is not part of the video: it is cut from the start at the moment the chats have loaded.
await page.goto(URL)
await click('button:has-text("Try the demo")')
await chat('Mark Dizon').waitFor({ timeout: 120_000 })
await page.waitForTimeout(500) // the list's previews and avatars settle
const cut = (Date.now() - t0) / 1000
console.log(`video starts at ${cut.toFixed(1)}s (signed in, chats loaded)`)
// The video's clock starts a little after the page opens, so the cut lands slightly late: hold the loaded list on screen first.
await page.waitForTimeout(1500)

if (flow === 'A') {
  // Mark is played by AI and answers on his own after each seller message. Wait for his answer to land: the request that
  // writes it, then the typing dots gone, and, when he sends a receipt, one more screenshot in the chat.
  const markAnswers = async (action, receipt = false) => {
    const shots = await page.locator('.chat .media-open').count()
    const done = page.waitForResponse((r) => r.url().includes('/customer/auto') && r.request().method() === 'POST', { timeout: 120_000 })
    await action()
    await done
    await page.locator('.messages li.typing').waitFor({ state: 'detached', timeout: 30_000 })
    if (receipt) await page.waitForFunction((n) => document.querySelectorAll('.chat .media-open').length > n, shots, { timeout: 30_000 })
  }
  await step('Open Mark: AI brief, and an alert: 2 to verify', async () => {
    await chat('Mark Dizon').click()
    await page.locator('.brief .brief-text p:not(.muted)').waitFor({ timeout: 90_000 })
    await page.locator('.checks-alert').waitFor({ timeout: 90_000 })
  }, 4000)
  await step('Click the alert: what was said, what to do, the records', async () => {
    await click('.checks-alert')
    await page.waitForTimeout(800)
  }, 6000)
  await step('Create draft order (cited to his messages)', async () => {
    await pane().evaluate((el) => el.scrollTo(0, 0))
    await click(newOrder)
    await page.locator('.verify-head:has-text("Draft")').waitFor({ timeout: 90_000 })
  }, 3000)
  await step('Send for confirmation in chat: Mark taps Confirm', async () => {
    await markAnswers(() => click('button:has-text("Send for confirmation in chat")'))
    await page.locator('.gates li:has-text("By the customer")').waitFor({ timeout: 30_000 })
  })
  await step('Already paid by GCash: Mark sends a receipt paid to someone else', () =>
    markAnswers(() => click('button:has-text("Already paid by GCash")'), true),
  )
  // The newest screenshot in the chat, enlarged: not checked yet.
  const open = async () => {
    await page.locator('.chat .media-open').last().click()
    await page.locator('dialog.viewer[open]').waitFor({ timeout: 15_000 })
    await page.locator('dialog.viewer[open] h2:has-text("Not checked yet")').waitFor({ timeout: 15_000 })
  }
  // Checked from the viewer itself: its results fill in beside the screenshot.
  const check = async () => {
    await click('dialog.viewer[open] button:has-text("Check receipt screenshots")')
    await page.waitForFunction(() => /Pass|Does not fit|fail/i.test(document.querySelector('dialog.viewer[open]')?.textContent ?? ''), null, { timeout: 120_000 })
  }
  const close = () => click('dialog.viewer[open] .viewer-close')
  await step('Open his screenshot in the chat: not checked yet', open, 2500)
  await step('Check receipt screenshots: the recipient does not fit', check, 6000)
  await step('Close', close, 800)
  await step('The seller replies; Mark sends a matching receipt', async () => {
    const form = page.locator('form.reply').first()
    await form.locator('textarea').click()
    await form.locator('textarea').pressSequentially(
      'Hi Mark! This receipt shows a different recipient name and number, not our GCash account. Could you check and send the receipt for your payment to us?',
      { delay: 18 },
    )
    await markAnswers(() => form.locator('button:has-text("Send")').click(), true)
  }, 3000)
  await step('Open the new screenshot: not checked yet', open, 2500)
  await step('Check it: it passes, but is still only a claim', check, 6000)
  await step('Close', close, 800)
  await step('Sample history screenshot (the seller finds it)', async () => {
    await click('button:has-text("Sample history screenshot")')
    await page.waitForFunction(() => {
      const label = [...document.querySelectorAll('.verify label')].find((l) => l.textContent.includes('Reference number in your GCash history'))
      return !!label?.querySelector('input')?.value
    }, null, { timeout: 90_000 })
  }, 3000)
  await step('Confirm GCash payment: payment verified', async () => {
    await page.locator('.verify input[type=checkbox]').first().check()
    const reason = page.locator('.verify label:has-text("Why are you confirming") input')
    if (await reason.count()) await reason.fill('Found in our GCash history')
    await click('button:has-text("Confirm GCash payment")')
    await page.locator('.gates li:has-text("GCash found in your own history")').waitFor({ timeout: 30_000 })
    await pane().evaluate((el) => el.scrollTo(0, 0))
  }, 3000)
  await step('The checks settle: none open', async () => {
    await page.locator('section.signals .pill:has-text("None open")').waitFor({ timeout: 90_000 })
    await show('section.signals')
  }, 5000)
} else {
  await step('Open Dan: AI brief (new customer, Binance, code)', async () => {
    await chat('Dan Cortez').click()
    await page.locator('.brief .brief-text p:not(.muted)').waitFor({ timeout: 90_000 })
  }, 5000)
  await step('Click the alert: his checks before shipping', async () => {
    await page.locator('.checks-alert').waitFor({ timeout: 90_000 })
    await click('.checks-alert')
    await page.waitForTimeout(800)
  }, 4000)
  await step('The code request: what to do', async () => {
    const card = page.locator('section.signals li.to-verify').filter({ hasText: 'Asked you to open an account' }).first()
    await card.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' }))
    await page.waitForTimeout(800)
  }, 5000)
  await step('Open Joy: fake seller line and an instruction', async () => {
    await chat('Joy Santos').click()
    await page.locator('.brief .brief-text p:not(.muted)').waitFor({ timeout: 90_000 })
  }, 4000)
  await step('Create draft order: seller price kept, instruction ignored', async () => {
    await click(newOrder)
    await page.locator('.verify-head:has-text("Draft")').waitFor({ timeout: 90_000 })
    await pane().evaluate((el) => el.scrollTo(0, 0))
  }, 5000)
  await step('Her checks: the payment claim is not proof', async () => {
    await page.locator('section.signals .pill').waitFor({ timeout: 90_000 })
    await show('section.signals')
  }, 5000)
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
