import { Component, type ReactNode } from 'react'

/** Catches a rendering error in one tab, so the rest of the app keeps working instead of the whole page going blank. */
export default class Boundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {}

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="page">
        <section className="order" role="alert">
          <h2>Something went wrong on this page</h2>
          <p>Other tabs still work. Reloading usually fixes it; your chats and orders are saved.</p>
          <p className="muted">{this.state.error.message}</p>
          <p className="actions">
            <button className="primary" onClick={() => location.reload()}>
              Reload
            </button>
          </p>
        </section>
      </div>
    )
  }
}
