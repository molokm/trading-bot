from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class OKXCredentials(BaseModel):
    api_key: str
    secret_key: str
    passphrase: str
    demo: bool = True
    permissions: List[str] = ["read"]


class ConnectionStatus(BaseModel):
    connected: bool
    demo: bool
    message: str


class BalanceDetail(BaseModel):
    ccy: str
    eq: str
    eqUsd: str
    availBal: str
    frozenBal: str


class PortfolioSummary(BaseModel):
    totalEqUsd: float
    details: List[BalanceDetail]
    pnl24h: Optional[float] = None
    pnl24hPct: Optional[float] = None


class PositionItem(BaseModel):
    instId: str
    instType: str
    posSide: str
    pos: str
    avgPx: str
    markPx: str
    upl: str
    uplRatio: str
    lever: str
    margin: str
    liqPx: Optional[str] = None
    ccy: str


class StrategyMeta(BaseModel):
    id: str
    name: str
    description: str
    timeframe: str
    symbol: str
    filename: str
    uploaded_at: str


class BacktestRequest(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 10000.0
    params: dict = {}


class BacktestResult(BaseModel):
    strategy_name: str
    symbol: str
    timeframe: str
    period: str
    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    equity_curve: List[dict]
    trades: List[dict]


class LiveDeployRequest(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str
    capital: float = 100.0
    params: dict = {}
    name: str = None


class OrderRecord(BaseModel):
    id: str
    strategy_id: str
    instId: str
    side: str
    sz: str
    px: Optional[str]
    ordType: str
    state: str
    filledSz: str
    pnl: Optional[str]
    timestamp: str


class TradeLog(BaseModel):
    orders: List[OrderRecord]
