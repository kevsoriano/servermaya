"""Sample GCash files for demo chats: a receipt screenshot and a transaction history export, both made up.

They are generated when asked for, dated now, so the real checks run on them: the receipt is read by Gemini and checked in
code like any customer screenshot, and the history PDF goes through the same parser and matching as a real export. Every
receipt is stamped as a sample, and the account they pay is the demo's fictional one, so they are no use as a fake.
"""

import io
import secrets
from datetime import datetime

import pypdf
from PIL import Image, ImageDraw, ImageFont

from receipts import PHT

# The demo store's fictional GCash account, as GCash would show it on a receipt.
PAY_TO_NAME, PAY_TO_MOBILE = "JU*N D**A C.", "+63 917 123 ****"
OTHER_NAME, OTHER_MOBILE = "MA**A S****S", "+63 918 555 ****"  # someone else entirely
PASSWORD = "delacruz4567"  # GCash's rule: last name and the last 4 digits of the mobile number

KINDS = ("matching", "wrong_amount", "wrong_recipient")


def sender(name: str) -> str:
    """A customer's name the way GCash history shows a sender: "Ana Reyes" becomes "AN* R."."""
    words = name.upper().split() or ["?"]
    return " ".join([words[0][:2] + "*" * max(len(words[0]) - 2, 1)] + [w[0] + "." for w in words[1:]])


def new_reference() -> str:
    return "10" + "".join(secrets.choice("0123456789") for _ in range(11))  # 13 digits, like a GCash Ref No.


def money(centavos: int) -> str:
    return f"{centavos / 100:,.2f}"


def receipt_png(amount: int, reference: str, at: datetime, name: str, mobile: str) -> bytes:
    """A receipt laid out like GCash's "Sent via GCash" screen, with a SAMPLE stamp across it."""
    w, h = 720, 1100
    img = Image.new("RGB", (w, h), "#0a5fd8")
    d = ImageDraw.Draw(img)
    font = lambda size: ImageFont.load_default(size=size)
    d.text((w / 2, 70), "GCash", font=font(56), fill="white", anchor="mm")
    d.rounded_rectangle((40, 140, w - 40, h - 60), radius=24, fill="white")
    at = at.astimezone(PHT)
    rows = [
        ("Sent via GCash", None, 44, "#111"),
        (name, None, 36, "#0a5fd8"),
        (f"Mobile No.: {mobile}", None, 26, "#555"),
        ("", None, 20, "#111"),
        ("Amount", money(amount), 30, "#111"),
        ("Total Amount Sent", f"PHP {money(amount)}", 34, "#111"),
        ("", None, 20, "#111"),
        ("Ref No.", f"{reference[:4]} {reference[4:7]} {reference[7:]}", 28, "#333"),
        ("Date", f"{at:%b %d, %Y} {at:%I:%M %p}".replace(" 0", " "), 28, "#333"),
    ]
    y = 220
    for label, value, size, color in rows:
        if value is None:
            d.text((w / 2, y), label, font=font(size), fill=color, anchor="mm")
        else:
            d.text((80, y), label, font=font(size), fill=color, anchor="lm")
            d.text((w - 80, y), value, font=font(size), fill=color, anchor="rm")
        y += size + 40
    d.text((w / 2, h - 110), "SAMPLE RECEIPT - DEMO ONLY - NOT PROOF OF PAYMENT", font=font(22), fill="#c62828", anchor="mm")
    stamp = Image.new("RGBA", (w, 160), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((w / 2, 80), "SAMPLE", font=font(130), fill=(198, 40, 40, 70), anchor="mm")
    img.paste(stamp.rotate(30, expand=True), (0, 300), stamp.rotate(30, expand=True))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def history_png(amount: int, reference: str, at: datetime, sender: str) -> bytes:
    """One payment as the seller's own GCash app shows it in transaction history, with a SAMPLE stamp across it."""
    w, h = 720, 1000
    img = Image.new("RGB", (w, h), "#f2f4f8")
    d = ImageDraw.Draw(img)
    font = lambda size: ImageFont.load_default(size=size)
    d.rectangle((0, 0, w, 120), fill="#0a5fd8")
    d.text((w / 2, 60), "Transaction Details", font=font(40), fill="white", anchor="mm")
    d.rounded_rectangle((40, 160, w - 40, h - 80), radius=24, fill="white")
    at = at.astimezone(PHT)
    d.text((w / 2, 240), "Received GCash from", font=font(32), fill="#555", anchor="mm")
    d.text((w / 2, 300), sender, font=font(40), fill="#111", anchor="mm")
    d.text((w / 2, 400), f"+ PHP {money(amount)}", font=font(56), fill="#1b873f", anchor="mm")
    y = 520
    for label, value in [("Reference Number", f"{reference[:4]} {reference[4:7]} {reference[7:]}"),
                         ("Date and Time", f"{at:%b %d, %Y} {at:%I:%M %p}".replace(" 0", " ")), ("Status", "Received")]:
        d.text((80, y), label, font=font(28), fill="#555", anchor="lm")
        d.text((w - 80, y), value, font=font(28), fill="#111", anchor="rm")
        y += 80
    d.text((w / 2, h - 130), "SAMPLE HISTORY ENTRY - DEMO ONLY", font=font(22), fill="#c62828", anchor="mm")
    stamp = Image.new("RGBA", (w, 160), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((w / 2, 80), "SAMPLE", font=font(130), fill=(198, 40, 40, 70), anchor="mm")
    img.paste(stamp.rotate(30, expand=True), (0, 280), stamp.rotate(30, expand=True))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def statement_lines(credits: list[dict], start: datetime, end: datetime, opening: int = 200000) -> list[str]:
    """Rows in the layout of GCash's history export, with a running balance. credits: [{at, reference, amount, sender}]."""
    rows = [(start.replace(hour=9, minute=0), "Payment to MERALCO, Merchant Transaction Number: DEMO1", new_reference(), -150000)]
    rows += [(c["at"], f"Received GCash from {c['sender']}", c["reference"], c["amount"]) for c in credits]
    rows.append((start.replace(hour=13, minute=0), "Received GCash from RO** M.", new_reference(), 25000))  # an unrelated payment
    rows.sort(key=lambda r: r[0])
    lines = ["GCash Transaction History", f"{start:%Y-%m-%d} to {end:%Y-%m-%d}", "Date and Time Description Reference No. Debit Credit Balance",
             f"STARTING BALANCE {money(opening)}"]
    balance, debit, credit = opening, 0, 0
    for at, text, ref, moved in rows:
        balance += moved
        debit, credit = debit + max(-moved, 0), credit + max(moved, 0)
        lines.append(f"{at.astimezone(PHT):%Y-%m-%d %I:%M %p} {text} {ref} {money(abs(moved))} {money(balance)}")
    return lines + [f"ENDING BALANCE {money(balance)}", f"Total Debit {money(debit)}", f"Total Credit {money(credit)}"]


def statement_pdf(lines: list[str], password: str = PASSWORD) -> bytes:
    """A one-page PDF with one text line per row, encrypted like GCash's export."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 8 Tf 30 800 Td 12 TL " + " ".join(f"({esc(line)}) '" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    raw, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(raw)
    raw += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010} 00000 n \n".encode() for o in offsets)
    raw += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(raw)))
    writer.encrypt(password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
