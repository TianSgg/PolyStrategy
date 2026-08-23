import { useEffect, useState } from 'react'
import { apiFetch } from '../api'

type Level = { price: string; size: string }
type Book = {
  observed_at: string
  tick_size: string | number | null
  best_bid: Level | null
  best_ask: Level | null
  bid_levels: number
  ask_levels: number
  bids: Level[]
  asks: Level[]
}

type Market = {
  market_slug: string
  temperature_label: string
  yes_token_id: string
  no_token_id: string
  selected?: boolean
}

interface Props {
  market: Market
  liveBooks: Record<string, Book> | null
}

function levels(book: Book | null, side: 'ask' | 'bid'): Level[] {
  if (!book) return []
  const source = side === 'ask' ? book.asks : book.bids
  if (side === 'ask') {
    return [...source]
      .sort((a, b) => Number(a.price) - Number(b.price))
      .slice(0, 8)
      .reverse()
  }
  return [...source].sort((a, b) => Number(b.price) - Number(a.price)).slice(0, 8)
}

function maxSize(book: Book | null): number {
  return Math.max(1, ...[...levels(book, 'ask'), ...levels(book, 'bid')].map(l => Number(l.size)))
}

function depthWidth(level: Level, book: Book | null): string {
  return `${Math.max(4, (Number(level.size) / maxSize(book)) * 100)}%`
}

function cents(price: string, tickSize: string | number | null): string {
  const centValue = Number(price) * 100
  const tick = Number(tickSize)
  const decimals = Number.isFinite(tick) && tick > 0
    ? Math.max(0, Math.min(3, Math.ceil(-Math.log10(tick)) - 2))
    : 2
  return `${centValue.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: decimals })}¢`
}

function shares(size: string): string {
  return Number(size).toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function total(level: Level): string {
  return `$${(Number(level.price) * Number(level.size)).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function spread(book: Book | null): string {
  if (!book?.best_ask || !book?.best_bid) return '-'
  return `${Math.round((Number(book.best_ask.price) - Number(book.best_bid.price)) * 100)}¢`
}

function BookView({ book, tickSize }: { book: Book | null; tickSize: string | number | null }) {
  if (!book) return <div className="wm-empty">-</div>
  const askLevels = levels(book, 'ask')
  const bidLevels = levels(book, 'bid')
  return (
    <>
      <div className="wm-book-meta">
        <span>UTC: {book.observed_at}</span>
        <span>最小单位: {tickSize || '-'}</span>
        <span>卖 {book.ask_levels} / 买 {book.bid_levels}</span>
      </div>
      <section className="wm-depth-book">
        <div className="wm-depth-header">
          <span>价格</span>
          <span>数量</span>
          <span>总额</span>
          <span></span>
        </div>
        <div className="wm-depth-side wm-asks">
          <span className="wm-side-label">卖单</span>
          {askLevels.length === 0 && <div className="wm-empty-levels">无卖单</div>}
          {askLevels.map(level => (
            <div className="wm-depth-row wm-ask-row" key={`ask-${level.price}`}>
              <div className="wm-depth-bar" style={{ width: depthWidth(level, book) }} />
              <strong>{cents(level.price, tickSize)}</strong>
              <span>{shares(level.size)}</span>
              <span>{total(level)}</span>
            </div>
          ))}
        </div>
        <div className="wm-spread-row">价差: {spread(book)}</div>
        <div className="wm-depth-side wm-bids">
          <span className="wm-side-label">买单</span>
          {bidLevels.length === 0 && <div className="wm-empty-levels">无买单</div>}
          {bidLevels.map(level => (
            <div className="wm-depth-row wm-bid-row" key={`bid-${level.price}`}>
              <div className="wm-depth-bar" style={{ width: depthWidth(level, book) }} />
              <strong>{cents(level.price, tickSize)}</strong>
              <span>{shares(level.size)}</span>
              <span>{total(level)}</span>
            </div>
          ))}
        </div>
      </section>
    </>
  )
}

export default function WeatherOrderbook({ market, liveBooks }: Props) {
  const [books, setBooks] = useState<Record<string, Book>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState<'yes' | 'no'>('yes')

  const tabs = [
    { outcome: 'yes' as const, label: 'YES token', tokenId: market.yes_token_id },
    { outcome: 'no' as const, label: 'NO token', tokenId: market.no_token_id },
  ]

  const displayedBooks = liveBooks || books

  async function refresh() {
    setLoading(true)
    setError('')
    try {
      const results = await Promise.all(
        tabs.map(async tab => {
          const res = await apiFetch(`/api/weather/orderbook/${encodeURIComponent(tab.tokenId)}`)
          if (!res.ok) throw new Error('Failed')
          return [tab.outcome, await res.json()] as [string, Book]
        })
      )
      setBooks(Object.fromEntries(results))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load orderbook')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [market.yes_token_id, market.no_token_id])

  return (
    <div className="wm-orderbook">
      <div className="wm-orderbook-toolbar">
        <div className="wm-orderbook-toolbar-info">
          <strong>{market.temperature_label}</strong>
          <span>{market.market_slug}</span>
        </div>
        <button className="wm-btn" onClick={refresh} disabled={loading}>
          {loading ? '...' : '刷新'}
        </button>
      </div>
      {error && <div className="wm-error">{error}</div>}
      {!error && (
        <>
          <div className="wm-tabs">
            {tabs.map(tab => (
              <button
                key={tab.outcome}
                className={`wm-tab ${activeTab === tab.outcome ? 'active' : ''}`}
                onClick={() => setActiveTab(tab.outcome)}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <BookView
            book={displayedBooks[activeTab] || null}
            tickSize={displayedBooks[activeTab]?.tick_size ?? null}
          />
        </>
      )}
    </div>
  )
}
