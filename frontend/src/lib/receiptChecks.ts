import { peso } from './format'

/** A customer's GCash screenshot as Gemini transcribed it, and the checks code ran on it. Shared by the order panel and the image viewer. */
export type Check = { check: string; result: 'pass' | 'fail' | 'unreadable'; detail: string }
export type Receipt = {
  readable: boolean
  outcome: 'successful' | 'failed' | 'pending' | 'not_shown'
  amount_php: number | null
  reference: string | null
  paid_at: string | null
  recipient_name: string | null
  recipient_mobile: string | null
  recipient_account?: string | null
}
export type Claim = { id: string; message_id: string; received_at: string; reference: string | null; receipt: Receipt; checks: Check[] }

export const RESULT = { pass: 'Pass', fail: 'Does not fit', unreadable: 'Could not read' }

export const LABEL: Record<string, string> = {
  receipt: 'Receipt',
  outcome: 'Payment went through',
  amount: 'Amount',
  time: 'Date and time',
  reference: 'Reference number',
  recipient_name: 'Recipient name',
  recipient_mobile: 'Recipient number or account',
  duplicate: 'Not a copy',
}

/** What the screenshot shows for each check, as Gemini copied it down. */
export function shown(r: Receipt, check: string) {
  const value = {
    outcome: r.outcome === 'not_shown' ? null : r.outcome,
    amount: r.amount_php === null ? null : peso(Math.round(r.amount_php * 100)),
    time: r.paid_at?.replace('T', ' '),
    reference: r.reference,
    recipient_name: r.recipient_name,
    recipient_mobile: r.recipient_mobile ?? r.recipient_account,
  }[check]
  return value === undefined ? '' : (value ?? 'nothing readable')
}
