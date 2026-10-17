import { useEffect, useState } from 'react'
import { api, load } from '../lib/firebase'
import { peso } from '../lib/format'
import { Photo } from '../orders/Packing'

type Variant = { name: string | null; price: number; stock: number | null; photo?: { path: string } | null }
type Product = { id: string; name: string; variants: Variant[] }
type FormVariant = { name: string; price: string; stock: string }
type Form = { id?: string; name: string; variants: FormVariant[] }

const blank: FormVariant = { name: '', price: '', stock: '' }
const toForm = (p: Product): Form => ({
  id: p.id,
  name: p.name,
  variants: p.variants.map((v) => ({ name: v.name ?? '', price: String(v.price / 100), stock: v.stock === null ? '' : String(v.stock) })),
})

/** The shop's products: drafts use each variant's price when no message states one, and note short stock; the packing
 * check compares photos with each variant's reference photo. Demo visitors see the demo shop's catalog, read-only. */
export default function Catalog({ readOnly }: { readOnly: boolean }) {
  const [products, setProducts] = useState<Product[]>()
  const [form, setForm] = useState<Form>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () => load<Product[]>('/catalog').then(setProducts, (e) => setError(e.message))
  useEffect(() => {
    refresh()
  }, [])

  const run = (p: Promise<unknown>) => {
    setBusy(true)
    setError('')
    return p
      .then(refresh)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }
  const save = (e: React.FormEvent) => {
    e.preventDefault()
    if (!form) return
    const body = {
      name: form.name,
      variants: form.variants.map((v) => ({
        name: v.name.trim() || null,
        price: Math.round(Number(v.price) * 100),
        stock: v.stock.trim() === '' ? null : Number(v.stock),
      })),
    }
    const init = { method: form.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    run(load(form.id ? `/catalog/${form.id}` : '/catalog', init).then(() => setForm(undefined)))
  }
  const upload = (p: Product, n: number, file: File) =>
    run(
      api(`/catalog/${p.id}/photos/${n}`, { method: 'POST', headers: { 'Content-Type': file.type }, body: file }).then((r) =>
        r.ok ? r : r.json().then((b) => Promise.reject(new Error(b.detail ?? 'Upload failed'))),
      ),
    )
  const remove = (p: Product) =>
    confirm(`Delete ${p.name}? Orders already drafted keep their copy.`) && run(load(`/catalog/${p.id}`, { method: 'DELETE' }))
  const variant = (n: number, patch: Partial<FormVariant>) =>
    setForm((f) => f && { ...f, variants: f.variants.map((v, k) => (k === n ? { ...v, ...patch } : v)) })

  return (
    <section className="order catalog">
      <h2>Catalog</h2>
      <p>
        Drafts use a product's price when no message states one, and note when a chat price differs or stock is short. The packing check
        compares photos with each variant's reference photo. Use photos of your own products.
      </p>
      {error && <p role="alert">{error}</p>}
      {!products && !error && <p className="muted">Loading…</p>}
      {products?.length === 0 && <p className="muted">No products yet.</p>}
      {products?.map((p) => (
        <article key={p.id} className="product">
          <h3>
            {p.name}
            {!readOnly && (
              <>
                {' '}
                <button onClick={() => setForm(toForm(p))} disabled={busy}>
                  Edit
                </button>{' '}
                <button onClick={() => remove(p)} disabled={busy}>
                  Delete
                </button>
              </>
            )}
          </h3>
          <div className="variants">
            {p.variants.map((v, n) => (
              <div key={n} className="variant">
                {v.photo ? (
                  <Photo path={`/catalog/${p.id}/photos/${n}`} alt={`${p.name} ${v.name ?? ''}`} caption="" />
                ) : (
                  <div className="placeholder">No photo</div>
                )}
                <strong>{v.name ?? 'One variant'}</strong>
                <span>
                  {peso(v.price)} · {v.stock === null ? 'stock not tracked' : `${v.stock} in stock`}
                </span>
                {!readOnly && (
                  <label className="upload">
                    {v.photo ? 'Replace photo' : 'Add photo'}
                    <input type="file" accept="image/*" hidden onChange={(e) => e.target.files?.[0] && upload(p, n, e.target.files[0])} />
                  </label>
                )}
              </div>
            ))}
          </div>
        </article>
      ))}
      {!readOnly && !form && (
        <button onClick={() => setForm({ name: '', variants: [{ ...blank }] })} disabled={busy}>
          Add product
        </button>
      )}
      {form && (
        <form onSubmit={save}>
          <label>
            Product name
            <input required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          {form.variants.map((v, n) => (
            <fieldset key={n}>
              <label>
                Variant (colour, size; blank if none)
                <input maxLength={60} value={v.name} onChange={(e) => variant(n, { name: e.target.value })} />
              </label>
              <label>
                Price in pesos
                <input required inputMode="decimal" value={v.price} onChange={(e) => variant(n, { price: e.target.value })} />
              </label>
              <label>
                Stock (blank if you don't track it)
                <input inputMode="numeric" value={v.stock} onChange={(e) => variant(n, { stock: e.target.value })} />
              </label>
              {form.variants.length > 1 && (
                <button type="button" onClick={() => setForm({ ...form, variants: form.variants.filter((_, k) => k !== n) })}>
                  Remove variant
                </button>
              )}
            </fieldset>
          ))}
          <p className="actions">
            <button type="button" onClick={() => setForm({ ...form, variants: [...form.variants, { ...blank }] })}>
              Add variant
            </button>
            <button className="primary" disabled={busy}>
              Save
            </button>
            <button type="button" onClick={() => setForm(undefined)}>
              Cancel
            </button>
          </p>
        </form>
      )}
    </section>
  )
}
