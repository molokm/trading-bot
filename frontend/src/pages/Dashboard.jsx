import React, { useState, useEffect } from 'react'
import { Wallet, TrendingUp, TrendingDown, Activity, BarChart3, Zap, ArrowUpRight, ArrowDownRight, ScrollText, DollarSign, Bot, XCircle, Loader2 } from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

function StatCard({ label, value, change, icon: Icon, positive }) {
  return (
    <div className="glass p-5 flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400 font-medium uppercase tracking-wider">{label}</span>
        <Icon size={16} className={positive ? 'text-neon-green' : 'text-neon-red'} />
      </div>
      <span className="text-2xl font-bold text-white">{value}</span>
      {change != null && (
        <span className={`text-xs flex items-center gap-1 ${positive ? 'text-neon-green' : 'text-neon-red'}`}>
          {positive ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
          {change}
        </span>
      )}
    </div>
  )
}

export default function Dashboard({ health, connected, isGuest }) {
  const { t } = useTranslation()
  const [portfolio, setPortfolio] = useState(null)
  const [positions, setPositions] = useState([])
  const [ticker, setTicker] = useState(null)
  const [momentumStatus, setMomentumStatus] = useState(null)
  const [momentumTrades, setMomentumTrades] = useState([])
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  const [closing, setClosing] = useState(null)
  const [loading, setLoading] = useState(true)
  const [momConfigForm, setMomConfigForm] = useState({
    risk_per_trade: 0.03,
    max_positions: 4,
    poll_interval_sec: 60,
    trail_pct: 0.015,
    breakeven_pct: 0.005,
    tp1_pct: 0.02,
    tp1_frac: 0.75,
    sl1_pct: 1.0,
    sl1_frac: 0.5,
    adx_threshold: 20,
  })

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 10000)
    return () => clearInterval(interval)
  }, [connected])

  async function loadData() {
    if (!connected) { setLoading(false); return }
    try {
      const [pf, pos, tk, momStatus, momTrades, trades, pnlData] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.getTicker('BTC-USDT-SWAP').catch(() => null),
        api.momentumStatus().catch(() => null),
        api.momentumTrades(10).catch(() => null),
        api.getPairedTrades(15).catch(() => null),
        api.getPnl().catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (tk) setTicker(tk)
      if (momStatus) {
        setMomentumStatus(momStatus)
        if (momStatus.config) {
          setMomConfigForm(prev => ({
            ...prev,
            risk_per_trade: momStatus.config.risk_per_trade ?? prev.risk_per_trade,
            max_positions: momStatus.config.max_positions ?? prev.max_positions,
            poll_interval_sec: momStatus.config.poll_interval_sec ?? prev.poll_interval_sec,
            trail_pct: momStatus.config.trail_pct ?? prev.trail_pct,
            breakeven_pct: momStatus.config.breakeven_pct ?? prev.breakeven_pct,
            tp1_pct: momStatus.config.tp1_pct ?? prev.tp1_pct,
            tp1_frac: momStatus.config.tp1_frac ?? prev.tp1_frac,
            adx_threshold: momStatus.config.adx_threshold ?? prev.adx_threshold,
          }))
        }
      }
      if (momTrades) setMomentumTrades(momTrades.trades || [])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData) setPnl(pnlData)
    } catch {}
    setLoading(false)
  }

  const btcUsd = ticker ? parseFloat(ticker.last) : 0
  const btcChange = ticker ? parseFloat(ticker.change24h || 0).toFixed(2) : '0.00'
  const btcPositive = parseFloat(btcChange) >= 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">{t('dashboard.title')}</h2>
          <p className="text-sm text-gray-400 mt-1">{t('dashboard.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-gray-400" />
          <span className="text-xs text-gray-500">
            {connected ? t('dashboard.websocket') : t('dashboard.websocket_offline')}
          </span>
          <span className={`status-dot ${connected ? 'online' : 'offline'}`} />
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label={t('dashboard.portfolio_value')}
          value={portfolio ? `$${(portfolio.totalEqUsd || 0).toLocaleString()}` : '---'}
          icon={Wallet}
          positive
        />
        <StatCard
          label={t('dashboard.btc_price')}
          value={btcUsd ? `$${btcUsd.toLocaleString()}` : '---'}
          change={`${btcChange}%`}
          icon={TrendingUp}
          positive={btcPositive}
        />
        <StatCard
          label={t('dashboard.open_positions')}
          value={positions.length}
          icon={BarChart3}
          positive={positions.length > 0}
        />
        <StatCard
          label={t('dashboard.status')}
          value={connected ? t('dashboard.online') : t('dashboard.offline')}
          icon={Zap}
          positive={connected}
        />
      </div>

      {/* PNL Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-blue/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-blue" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_day')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['1d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['1d'] >= 0 ? '+' : ''}$${pnl['1d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-purple/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-purple" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_week')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['7d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['7d'] >= 0 ? '+' : ''}$${pnl['7d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-green/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-green" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_month')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['30d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['30d'] >= 0 ? '+' : ''}$${pnl['30d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
        <div className="glass p-5 flex items-center gap-4">
          <div className={`w-10 h-10 rounded-lg ${(pnl?.unrealized || 0) >= 0 ? 'bg-neon-green/10' : 'bg-neon-red/10'} flex items-center justify-center`}>
            <DollarSign size={20} className={`${(pnl?.unrealized || 0) >= 0 ? 'text-neon-green' : 'text-neon-red'}`} />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">UNREALIZED</div>
            <div className={`text-xl font-bold ${(pnl?.unrealized || 0) >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${(pnl.unrealized || 0) >= 0 ? '+' : ''}$${(pnl.unrealized || 0).toFixed(2)}` : '---'}
            </div>
          </div>
        </div>
      </div>

      {/* Portfolio Details */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <Wallet size={16} className="text-neon-green" />
          {t('dashboard.portfolio_balance')}
        </h3>
        {portfolio ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-white/5">
                  <th className="text-left py-3 px-2 font-medium">{t('dashboard.asset')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.balance')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.usd_value')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.available')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.frozen')}</th>
                </tr>
              </thead>
              <tbody>
                {(portfolio.details || []).map(d => (
                  <tr key={d.ccy} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                    <td className="py-3 px-2 font-medium text-white">{d.ccy}</td>
                    <td className="py-3 px-2 text-right mono">{parseFloat(d.eq).toFixed(4)}</td>
                    <td className="py-3 px-2 text-right mono text-neon-green">
                      ${parseFloat(d.eqUsd).toLocaleString()}
                    </td>
                    <td className="py-3 px-2 text-right mono">{parseFloat(d.availBal).toFixed(4)}</td>
                    <td className="py-3 px-2 text-right mono text-gray-400">{parseFloat(d.frozenBal).toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-8 text-gray-500">
            <p>{t('dashboard.no_portfolio')}</p>
          </div>
        )}
      </div>

      {/* Momentum Strategy */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <TrendingUp size={16} className="text-neon-purple" />
          Momentum Strategy
          {momentumStatus && (
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              momentumStatus.running ? 'bg-neon-green/20 text-neon-green' : 'bg-gray-500/20 text-gray-400'
            }`}>
              {momentumStatus.running ? 'Активна' : 'Остановлена'}
            </span>
          )}
        </h3>
        <div className="text-xs text-gray-500 mb-3 leading-relaxed">
          Моментум на дневных свечах (ROC5>0, ROC50>0, EMA15>EMA30, ADX>20, PDI>MDI).
          Выход: трейлинг 1.5% → каскадный стоп -1%/50% → безубыток +0.5% → TP1 +2%/50%.
        </div>
        {momentumStatus && momentumStatus.running ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
              <div className="bg-white/5 rounded-lg px-3 py-2">
                <span className="text-gray-400">Капитал:</span>
                <div className="text-neon-green font-medium mt-0.5">${momentumStatus.equity?.toLocaleString() || '---'}</div>
              </div>
              <div className="bg-white/5 rounded-lg px-3 py-2">
                <span className="text-gray-400">Позиций:</span>
                <div className="text-white font-medium mt-0.5">{momentumStatus.open_positions?.length || 0} / {momentumStatus.config?.max_positions || 4}</div>
              </div>
              <div className="bg-white/5 rounded-lg px-3 py-2">
                <span className="text-gray-400">Сделок:</span>
                <div className="text-white font-medium mt-0.5">{momentumStatus.total_trades || 0}</div>
              </div>
              <div className="bg-white/5 rounded-lg px-3 py-2">
                <span className="text-gray-400">Риск:</span>
                <div className="text-white font-medium mt-0.5">{((momentumStatus.config?.risk_per_trade || 0.03) * 100).toFixed(0)}%</div>
              </div>
            </div>

            {momentumStatus.open_positions?.length > 0 && (
              <div>
                <div className="text-xs text-gray-400 font-medium mb-2">Открытые позиции</div>
                <div className="space-y-1">
                  {momentumStatus.open_positions.map((p, i) => (
                    <div key={i} className="flex items-center justify-between text-xs bg-white/5 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="px-1.5 py-0.5 rounded font-bold bg-neon-green/20 text-neon-green">LONG</span>
                        <span className="text-white font-medium">{p.symbol}</span>
                        <span className="text-gray-400">entry=${p.entry?.toFixed(2)}</span>
                      </div>
                      <div className="flex items-center gap-3 text-gray-400">
                        <span>stop=${p.stop?.toFixed(2)}</span>
                        <span>peak={p.peak_ratio}x</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {momentumTrades.length > 0 && (
              <div>
                <div className="text-xs text-gray-400 font-medium mb-2">Последние сделки</div>
                <div className="space-y-1">
                  {momentumTrades.slice(0, 5).map((tr, i) => (
                    <div key={i} className="flex items-center justify-between text-xs bg-white/5 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className={`px-1.5 py-0.5 rounded font-bold ${
                          tr.side === 'buy' ? 'bg-neon-green/20 text-neon-green' : 'bg-neon-red/20 text-neon-red'
                        }`}>
                          {tr.side === 'buy' ? 'LONG' : tr.reason ? `EXIT ${tr.reason}` : 'EXIT'}
                        </span>
                        <span className="text-white font-medium">{tr.symbol}</span>
                        {tr.pnl != null && (
                          <span className={`font-bold ${tr.pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                            ${tr.pnl >= 0 ? '+' : ''}{tr.pnl.toFixed(2)}
                          </span>
                        )}
                      </div>
                      <span className="text-gray-500">{tr.time ? new Date(tr.time).toLocaleString() : ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="flex items-center gap-2 mt-2">
              <button
                onClick={async () => {
                  try {
                    await api.momentumStop()
                    loadData()
                  } catch (e) {
                    alert('Ошибка: ' + e.message)
                  }
                }}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-neon-red/10 border border-neon-red/20 text-neon-red hover:bg-neon-red/20 transition-colors"
              >
                Остановить
              </button>
            </div>
          </div>
        ) : (
          <div className="text-center py-4">
            <p className="text-sm text-gray-500 mb-3">Стратегия не запущена</p>
            <div className="max-w-md mx-auto text-left mb-4 space-y-2">
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Риск на сделку</span>
                <input type="range" min="0.5" max="10" step="0.5"
                  value={(momConfigForm.risk_per_trade * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, risk_per_trade: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{(momConfigForm.risk_per_trade * 100).toFixed(0)}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Макс. позиций</span>
                <input type="range" min="1" max="8" step="1"
                  value={momConfigForm.max_positions}
                  onChange={e => setMomConfigForm(f => ({...f, max_positions: Number(e.target.value)}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{momConfigForm.max_positions}</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Трейлинг стоп</span>
                <input type="range" min="0.5" max="10" step="0.5"
                  value={(momConfigForm.trail_pct * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, trail_pct: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{(momConfigForm.trail_pct * 100).toFixed(0)}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Безубыток при</span>
                <input type="range" min="0.1" max="3" step="0.1"
                  value={(momConfigForm.breakeven_pct * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, breakeven_pct: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{(momConfigForm.breakeven_pct * 100).toFixed(0)}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">TP1 частичное при</span>
                <input type="range" min="0.5" max="8" step="0.5"
                  value={(momConfigForm.tp1_pct * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, tp1_pct: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{(momConfigForm.tp1_pct * 100).toFixed(0)}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">TP1 размер закрытия</span>
                <input type="range" min="20" max="100" step="5"
                  value={(momConfigForm.tp1_frac * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, tp1_frac: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{(momConfigForm.tp1_frac * 100).toFixed(0)}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">SL1 каскад при</span>
                <input type="range" min="0" max="5" step="0.5"
                  value={(momConfigForm.sl1_pct || 0).toFixed(1)}
                  onChange={e => setMomConfigForm(f => ({...f, sl1_pct: parseFloat(e.target.value)}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{momConfigForm.sl1_pct || 0}%</span>
              </label>
              <label className="flex items-center justify-between text-xs">
                <span className="text-gray-400">SL1 размер закрытия</span>
                <input type="range" min="20" max="100" step="5"
                  value={((momConfigForm.sl1_frac || 0.5) * 100).toFixed(0)}
                  onChange={e => setMomConfigForm(f => ({...f, sl1_frac: e.target.value / 100}))}
                  className="w-32" />
                <span className="text-white font-mono w-12 text-right">{((momConfigForm.sl1_frac || 0.5) * 100).toFixed(0)}%</span>
              </label>
            </div>
            <button
              onClick={async () => {
                try {
                  await api.momentumStart(momConfigForm)
                  loadData()
                } catch (e) {
                  alert('Ошибка: ' + e.message)
                }
              }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-neon-green/10 border border-neon-green/20 text-neon-green hover:bg-neon-green/20 transition-colors"
            >
              Запустить Momentum
            </button>
          </div>
        )}
      </div>

      {/* Recent Trades */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <ScrollText size={16} className="text-neon-blue" />
          {t('dashboard.recent_trades') || 'Последние сделки'}
        </h3>
        {tradeLog.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-white/5">
                  <th className="text-left py-2 px-1 font-medium">Время входа</th>
                  <th className="text-left py-2 px-1 font-medium">Выход</th>
                  <th className="text-left py-2 px-1 font-medium">Пара</th>
                  <th className="text-center py-2 px-1 font-medium">Направление</th>
                  <th className="text-right py-2 px-1 font-medium">Цена входа</th>
                  <th className="text-right py-2 px-1 font-medium">Цена выхода</th>
                  <th className="text-right py-2 px-1 font-medium">Объём</th>
                  <th className="text-right py-2 px-1 font-medium">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {tradeLog.slice(0, 20).map((t, i) => {
                  const pnl = t.pnl != null ? parseFloat(t.pnl) : null
                  return (
                  <tr key={t.signal_id || i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                    <td className="py-2 px-1 text-xs text-gray-400">
                      {t.entry_time ? new Date(t.entry_time).toLocaleString() : '-'}
                    </td>
                    <td className="py-2 px-1 text-xs text-gray-400">
                      {t.exit_time ? new Date(t.exit_time).toLocaleString() : 'открыта'}
                    </td>
                    <td className="py-2 px-1 text-white font-medium">{t.inst_id || '-'}</td>
                    <td className="py-2 px-1 text-center">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                        t.side === 'buy' ? 'bg-neon-green/10 text-neon-green' : 'bg-neon-red/10 text-neon-red'
                      }`}>
                        {t.side === 'buy' ? 'LONG' : 'SHORT'}
                      </span>
                    </td>
                    <td className="py-2 px-1 text-right mono">
                      {t.entry_px ? `$${parseFloat(t.entry_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-2 px-1 text-right mono">
                      {t.exit_px ? `$${parseFloat(t.exit_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-2 px-1 text-right mono">{t.entry_sz || '-'}</td>
                    <td className="py-2 px-1 text-right">
                      {pnl !== null ? (
                        <span className={`mono text-xs font-bold ${pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                          {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} USDT
                        </span>
                      ) : (
                        <span className="text-xs text-gray-500">открыта</span>
                      )}
                    </td>
                  </tr>
                )})}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-500">Сделок пока нет</p>
        )}
      </div>

      {/* Positions & Market Info */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="glass p-5">
          <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
            <TrendingUp size={16} className="text-neon-blue" />
            {t('dashboard.open_positions_title')}
          </h3>
          {positions.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-white/5">
                    <th className="text-left py-2 px-1 font-medium">{t('dashboard.pair')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.size')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.entry')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.mark')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.pnl')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.slice(0, 10).map((p, i) => {
                    const pnl = parseFloat(p.upl || 0)
                    const positive = pnl >= 0
                    const posId = `${p.instId}_${p.posSide}`
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                        <td className="py-2 px-1 text-white font-medium">{p.instId}</td>
                        <td className="py-2 px-1 text-right mono">{parseFloat(p.pos).toFixed(3)}</td>
                        <td className="py-2 px-1 text-right mono">${parseFloat(p.avgPx).toLocaleString()}</td>
                        <td className="py-2 px-1 text-right mono">${parseFloat(p.markPx).toLocaleString()}</td>
                        <td className={`py-2 px-1 text-right mono ${positive ? 'text-neon-green' : 'text-neon-red'}`}>
                          ${pnl.toLocaleString()}
                        </td>
                        <td className="py-2 px-1 text-right">
                          {isGuest ? (
                            <span className="text-xs text-gray-500">—</span>
                          ) : (
                          <button
                            onClick={async () => {
                              setClosing(posId)
                              try {
                                await api.closePosition(p.instId, p.posSide, p.pos, p.mgnMode || 'cross')
                                loadData()
                              } catch (e) {
                                alert('Ошибка закрытия: ' + e.message)
                              } finally {
                                setClosing(null)
                              }
                            }}
                            disabled={closing === posId}
                            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-neon-red/10 border border-neon-red/20 text-neon-red hover:bg-neon-red/20 transition-colors disabled:opacity-50"
                            title="Закрыть позицию"
                          >
                            {closing === posId ? <Loader2 size={12} className="animate-spin" /> : <XCircle size={12} />}
                            {closing === posId ? '...' : 'Закрыть'}
                          </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-8 text-gray-500">
              <p>{t('dashboard.no_positions')}</p>
            </div>
          )}
        </div>

        <div className="glass p-5">
          <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
            <BarChart3 size={16} className="text-neon-purple" />
            {t('dashboard.market_data')}
          </h3>
          {ticker ? (
            <div className="space-y-3">
              {[
                { label: t('dashboard.last_price'), value: `$${parseFloat(ticker.last).toLocaleString()}`, color: 'text-white' },
                { label: t('dashboard.bid'), value: `$${parseFloat(ticker.bid).toLocaleString()}`, color: 'text-neon-green' },
                { label: t('dashboard.ask'), value: `$${parseFloat(ticker.ask).toLocaleString()}`, color: 'text-neon-red' },
                { label: t('dashboard.high_24h'), value: `$${parseFloat(ticker.high24h).toLocaleString()}`, color: 'text-neon-green' },
                { label: t('dashboard.low_24h'), value: `$${parseFloat(ticker.low24h).toLocaleString()}`, color: 'text-neon-red' },
                { label: t('dashboard.change_24h'), value: `${btcChange}%`, color: btcPositive ? 'text-neon-green' : 'text-neon-red' },
                { label: t('dashboard.volume_24h'), value: ticker.vol24h ? `${parseFloat(ticker.vol24h).toFixed(2)} BTC` : '---', color: 'text-gray-300' },
              ].map(item => (
                <div key={item.label} className="flex justify-between items-center">
                  <span className="text-sm text-gray-400">{item.label}</span>
                  <span className={`mono text-sm font-medium ${item.color}`}>{item.value}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-8 text-gray-500">
              <p>{t('dashboard.no_market_data')}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
