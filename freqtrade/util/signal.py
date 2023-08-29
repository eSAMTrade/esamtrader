from pyxtension.models import ImmutableExtModel


class SignalV4(ImmutableExtModel):
    TP: int
    SL: int
    confidence: float
    last_agg_ts_ms: int
    last_close_agg_price: int
    local_ts_ms: int
    direction: int
    signal_provider: str
    pred_len_ms: int
    symbol: str
    signal_group: str
    tick_size: float = 0.0001