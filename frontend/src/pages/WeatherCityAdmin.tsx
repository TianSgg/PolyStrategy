import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '../api'

interface City {
  id: number
  city_name: string
  city_slug: string
  timezone: string
  has_highest_market: number
  has_lowest_market: number
  monitor_highest: number
  monitor_lowest: number
  enabled: number
  sort_order: number
}

interface FormData {
  city_name: string
  city_slug: string
  timezone: string
  has_highest_market: boolean
  has_lowest_market: boolean
  monitor_highest: boolean
  monitor_lowest: boolean
  enabled: boolean
  sort_order: number
}

const emptyForm: FormData = {
  city_name: '',
  city_slug: '',
  timezone: '',
  has_highest_market: false,
  has_lowest_market: false,
  monitor_highest: false,
  monitor_lowest: false,
  enabled: true,
  sort_order: 0,
}

const SLUG_TIMEZONE_MAP: Record<string, string> = {
  "taipei": "Asia/Taipei",
  "miami": "America/New_York",
  "kuala-lumpur": "Asia/Kuala_Lumpur",
  "paris": "Europe/Paris",
  "mexico-city": "America/Mexico_City",
  "nyc": "America/New_York",
  "panama-city": "America/Panama",
  "sao-paulo": "America/Sao_Paulo",
  "buenos-aires": "America/Argentina/Buenos_Aires",
  "lucknow": "Asia/Kolkata",
  "cape-town": "Africa/Johannesburg",
  "karachi": "Asia/Karachi",
  "london": "Europe/London",
  "wellington": "Pacific/Auckland",
  "tel-aviv": "Asia/Jerusalem",
  "tokyo": "Asia/Tokyo",
  "denver": "America/Denver",
  "manila": "Asia/Manila",
  "toronto": "America/Toronto",
  "amsterdam": "Europe/Amsterdam",
  "ankara": "Europe/Istanbul",
  "atlanta": "America/New_York",
  "austin": "America/Chicago",
  "beijing": "Asia/Shanghai",
  "berlin": "Europe/Berlin",
  "busan": "Asia/Seoul",
  "chengdu": "Asia/Shanghai",
  "chicago": "America/Chicago",
  "chongqing": "Asia/Shanghai",
  "dallas": "America/Chicago",
  "guangzhou": "Asia/Shanghai",
  "helsinki": "Europe/Helsinki",
  "hong-kong": "Asia/Hong_Kong",
  "houston": "America/Chicago",
  "istanbul": "Europe/Istanbul",
  "jeddah": "Asia/Riyadh",
  "jinan": "Asia/Shanghai",
  "los-angeles": "America/Los_Angeles",
  "madrid": "Europe/Madrid",
  "milan": "Europe/Rome",
  "moscow": "Europe/Moscow",
  "munich": "Europe/Berlin",
  "qingdao": "Asia/Shanghai",
  "san-francisco": "America/Los_Angeles",
  "seattle": "America/Los_Angeles",
  "shanghai": "Asia/Shanghai",
  "shenzhen": "Asia/Shanghai",
  "singapore": "Asia/Singapore",
  "warsaw": "Europe/Warsaw",
  "wuhan": "Asia/Shanghai",
  "zhengzhou": "Asia/Shanghai",
  "dubai": "Asia/Dubai",
  "mumbai": "Asia/Kolkata",
  "delhi": "Asia/Kolkata",
  "bangkok": "Asia/Bangkok",
  "sydney": "Australia/Sydney",
  "melbourne": "Australia/Melbourne",
  "jakarta": "Asia/Jakarta",
  "cairo": "Africa/Cairo",
  "lagos": "Africa/Lagos",
  "rome": "Europe/Rome",
  "lisbon": "Europe/Lisbon",
  "zurich": "Europe/Zurich",
  "stockholm": "Europe/Stockholm",
  "oslo": "Europe/Oslo",
  "copenhagen": "Europe/Copenhagen",
  "phoenix": "America/Phoenix",
  "portland": "America/Los_Angeles",
  "boston": "America/New_York",
  "philadelphia": "America/New_York",
  "detroit": "America/Detroit",
  "minneapolis": "America/Chicago",
  "las-vegas": "America/Los_Angeles",
  "new-york": "America/New_York",
  "rio-de-janeiro": "America/Sao_Paulo",
  "lima": "America/Lima",
  "bogota": "America/Bogota",
  "santiago": "America/Santiago",
  "hanoi": "Asia/Ho_Chi_Minh",
  "ho-chi-minh": "Asia/Ho_Chi_Minh",
  "seoul": "Asia/Seoul",
  "osaka": "Asia/Tokyo",
  "nagoya": "Asia/Tokyo",
}

function guessTimezone(slug: string): string {
  if (!slug) return ''
  const lower = slug.toLowerCase().trim()
  if (SLUG_TIMEZONE_MAP[lower]) return SLUG_TIMEZONE_MAP[lower]
  for (const [key, tz] of Object.entries(SLUG_TIMEZONE_MAP)) {
    if (lower.includes(key) || key.includes(lower)) return tz
  }
  return ''
}

interface Props {
  darkMode: boolean
  onBack: () => void
}

export default function WeatherCityAdmin({ darkMode, onBack }: Props) {
  const [cities, setCities] = useState<City[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState<FormData>(emptyForm)
  const [saving, setSaving] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [dirty, setDirty] = useState(false)

  const fetchCities = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch('/api/weather/cities/admin')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setCities(data.cities)
    } catch (e: any) {
      setError(e.message || 'Loading failed')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchCities() }, [fetchCities])

  const openCreate = () => {
    setEditingId(null)
    setForm(emptyForm)
    setModalOpen(true)
  }

  const openEdit = (city: City) => {
    setEditingId(city.id)
    setForm({
      city_name: city.city_name,
      city_slug: city.city_slug,
      timezone: city.timezone,
      has_highest_market: !!city.has_highest_market,
      has_lowest_market: !!city.has_lowest_market,
      monitor_highest: !!city.monitor_highest,
      monitor_lowest: !!city.monitor_lowest,
      enabled: !!city.enabled,
      sort_order: city.sort_order,
    })
    setModalOpen(true)
  }

  const handleSlugChange = (slug: string) => {
    const tz = guessTimezone(slug)
    setForm(prev => ({
      ...prev,
      city_slug: slug,
      ...(tz && !prev.timezone ? { timezone: tz } : tz ? { timezone: tz } : {}),
    }))
  }

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      const url = editingId
        ? `/api/weather/cities/admin/${editingId}`
        : '/api/weather/cities/admin'
      const method = editingId ? 'PUT' : 'POST'
      const res = await apiFetch(url, { method, body: JSON.stringify(form) })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || `HTTP ${res.status}`)
      }
      setModalOpen(false)
      setDirty(true)
      await fetchCities()
    } catch (e: any) {
      setError(e.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (city: City) => {
    if (!confirm(`确定删除「${city.city_name}」(${city.city_slug}) 吗？`)) return
    try {
      const res = await apiFetch(`/api/weather/cities/admin/${city.id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setDirty(true)
      await fetchCities()
    } catch (e: any) {
      setError(e.message || 'Delete failed')
    }
  }

  const handleReload = async () => {
    setReloading(true)
    setError('')
    try {
      const res = await apiFetch('/api/weather/cities/reload', { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setDirty(false)
      alert('重载成功，新配置已生效')
    } catch (e: any) {
      setError(e.message || 'Reload failed')
    } finally {
      setReloading(false)
    }
  }

  const bg = darkMode ? '#1e293b' : '#ffffff'
  const text = darkMode ? '#f8fafc' : '#0f172a'
  const border = darkMode ? '#334155' : '#e2e8f0'
  const muted = darkMode ? '#94a3b8' : '#64748b'

  return (
    <div style={{ flex: 1, padding: '24px', overflow: 'auto', color: text }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <button onClick={onBack} style={btnStyle(darkMode ? '#475569' : '#e2e8f0', text)}>
            &larr; 返回
          </button>
          <h2 style={{ margin: 0, fontSize: 20 }}>城市管理</h2>
        </div>
        <div style={{ display: 'flex', gap: 12 }}>
          <button onClick={openCreate} style={btnStyle('#2563eb', '#fff')}>
            + 新增城市
          </button>
          <button
            onClick={handleReload}
            disabled={reloading}
            style={btnStyle(dirty ? '#dc2626' : '#059669', '#fff')}
          >
            {reloading ? '重载中...' : dirty ? '应用更改（重载服务）' : '重载服务'}
          </button>
        </div>
      </div>

      {error && <div style={{ color: '#ef4444', marginBottom: 12 }}>{error}</div>}

      {loading ? (
        <div style={{ color: muted }}>加载中...</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
            <thead>
              <tr style={{ borderBottom: `2px solid ${border}` }}>
                {['城市', 'Slug', '时区', '高温市场', '低温市场', '监听高温', '监听低温', '状态', '排序', '操作'].map(h => (
                  <th key={h} style={{ padding: '10px 8px', textAlign: 'left', color: muted, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cities.map(city => (
                <tr key={city.id} style={{ borderBottom: `1px solid ${border}` }}>
                  <td style={cellStyle}>{city.city_name}</td>
                  <td style={cellStyle}><code style={{ fontSize: 12 }}>{city.city_slug}</code></td>
                  <td style={cellStyle}><span style={{ fontSize: 12 }}>{city.timezone}</span></td>
                  <td style={cellStyle}>{city.has_highest_market ? 'Y' : '-'}</td>
                  <td style={cellStyle}>{city.has_lowest_market ? 'Y' : '-'}</td>
                  <td style={cellStyle}>{city.monitor_highest ? 'Y' : '-'}</td>
                  <td style={cellStyle}>{city.monitor_lowest ? 'Y' : '-'}</td>
                  <td style={cellStyle}>
                    <span style={{ color: city.enabled ? '#22c55e' : '#ef4444', fontWeight: 500 }}>
                      {city.enabled ? '启用' : '停用'}
                    </span>
                  </td>
                  <td style={cellStyle}>{city.sort_order}</td>
                  <td style={cellStyle}>
                    <button onClick={() => openEdit(city)} style={smallBtn(darkMode)}>编辑</button>{' '}
                    <button onClick={() => handleDelete(city)} style={{ ...smallBtn(darkMode), color: '#ef4444' }}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Modal */}
      {modalOpen && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}>
          <div style={{ background: bg, borderRadius: 12, padding: 28, width: 460, maxHeight: '80vh', overflow: 'auto', border: `1px solid ${border}` }}>
            <h3 style={{ margin: '0 0 20px', color: text }}>{editingId ? '编辑城市' : '新增城市'}</h3>

            <FormField label="城市名称" darkMode={darkMode}>
              <input value={form.city_name} onChange={e => setForm({ ...form, city_name: e.target.value })} style={inputStyle(darkMode)} placeholder="例如 Berlin" />
            </FormField>
            <FormField label="Slug（自动匹配时区）" darkMode={darkMode}>
              <input
                value={form.city_slug}
                onChange={e => handleSlugChange(e.target.value)}
                style={inputStyle(darkMode)}
                placeholder="输入 slug 自动填充时区"
              />
            </FormField>
            <FormField label="时区" darkMode={darkMode}>
              <input value={form.timezone} onChange={e => setForm({ ...form, timezone: e.target.value })} style={inputStyle(darkMode)} placeholder="由 slug 自动填充" />
            </FormField>
            <FormField label="排序值" darkMode={darkMode}>
              <input type="number" value={form.sort_order} onChange={e => setForm({ ...form, sort_order: parseInt(e.target.value) || 0 })} style={inputStyle(darkMode)} />
            </FormField>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, margin: '16px 0' }}>
              <CheckField label="有最高温市场" checked={form.has_highest_market} onChange={v => {
                const next = { ...form, has_highest_market: v }
                if (!v) next.monitor_highest = false
                setForm(next)
              }} />
              <CheckField label="有最低温市场" checked={form.has_lowest_market} onChange={v => {
                const next = { ...form, has_lowest_market: v }
                if (!v) next.monitor_lowest = false
                setForm(next)
              }} />
              <CheckField label="监听最高温" checked={form.monitor_highest} onChange={v => setForm({ ...form, monitor_highest: v })} disabled={!form.has_highest_market} />
              <CheckField label="监听最低温" checked={form.monitor_lowest} onChange={v => setForm({ ...form, monitor_lowest: v })} disabled={!form.has_lowest_market} />
              <CheckField label="启用" checked={form.enabled} onChange={v => setForm({ ...form, enabled: v })} />
            </div>

            {error && <div style={{ color: '#ef4444', fontSize: 13, marginBottom: 12 }}>{error}</div>}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
              <button onClick={() => setModalOpen(false)} style={btnStyle(darkMode ? '#475569' : '#e2e8f0', text)}>取消</button>
              <button onClick={handleSave} disabled={saving} style={btnStyle('#2563eb', '#fff')}>
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function FormField({ label, darkMode, children }: { label: string; darkMode: boolean; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 4, color: darkMode ? '#94a3b8' : '#64748b' }}>{label}</label>
      {children}
    </div>
  )
}

function CheckField({ label, checked, onChange, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, opacity: disabled ? 0.5 : 1, cursor: disabled ? 'default' : 'pointer' }}>
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} disabled={disabled} />
      {label}
    </label>
  )
}

function btnStyle(bg: string, color: string): React.CSSProperties {
  return { padding: '8px 16px', borderRadius: 6, border: 'none', background: bg, color, cursor: 'pointer', fontWeight: 500, fontSize: 14 }
}

function smallBtn(darkMode: boolean): React.CSSProperties {
  return { padding: '4px 10px', borderRadius: 4, border: 'none', background: darkMode ? '#334155' : '#e2e8f0', color: darkMode ? '#f8fafc' : '#0f172a', cursor: 'pointer', fontSize: 12 }
}

function inputStyle(darkMode: boolean): React.CSSProperties {
  return { width: '100%', padding: '8px 12px', borderRadius: 6, border: `1px solid ${darkMode ? '#475569' : '#cbd5e1'}`, background: darkMode ? '#0f172a' : '#fff', color: darkMode ? '#f8fafc' : '#0f172a', fontSize: 14, boxSizing: 'border-box' }
}

const cellStyle: React.CSSProperties = { padding: '10px 8px' }
