/** Where PayMongo sends a customer who paid from a link in the chat. Coming back here proves nothing: only PayMongo's
 * webhook, or the seller rechecking with PayMongo, marks an order paid. */
export function Paid() {
  return (
    <main className="order">
      <h1>Thank you!</h1>
      <p>Your payment is being checked with PayMongo. The seller will confirm it in your chat. You can close this page.</p>
    </main>
  )
}
