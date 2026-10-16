/** Display helpers: money, phone numbers, labels. */

export const peso = (centavos: number | null) =>
  centavos === null ? 'not stated' : (centavos / 100).toLocaleString('en-PH', { style: 'currency', currency: 'PHP' })

/** 639171231234 -> 0917 123 1234 */
export const localMobile = (m: string) => `0${m.slice(2, 5)} ${m.slice(5, 8)} ${m.slice(8)}`

export const STATUS = {
  draft: 'Draft',
  awaiting_confirmation: 'Waiting for customer to confirm',
  confirmed: 'Confirmed',
  cancelled: 'Cancelled',
}

export const CLAIM_TYPES: Record<string, string> = {
  missing_item: 'Missing item',
  wrong_item: 'Wrong item',
  damage: 'Damaged',
  nonreceipt: 'Not received',
  payment_issue: 'Payment issue',
  cancellation: 'Cancellation',
  other: 'Other',
}

/** Orders drafted from a chat, real Messenger or demo, have messages to check and a thread to reply in. */
export const fromChat = (o: { source?: { type: string } }) => o.source?.type === 'messenger' || o.source?.type === 'demo'
