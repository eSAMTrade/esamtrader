# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
# --- Do not remove these libs ---
import logging
from typing import Optional

import numpy as np  # noqa
import pandas as pd  # noqa
from ccxt import ROUND
from pandas import DataFrame
from tsx import TS

from freqtrade.constants import Config
from freqtrade.enums import SignalType, SignalTagType, SignalDirection, TradingMode
from freqtrade.enums.signaltype import EntrySignal
from freqtrade.enums.strategy_dataframe_types import ColumnNames
from freqtrade.exchange import timeframe_to_seconds
from freqtrade.strategy import (IStrategy)

# --------------------------------
# Add your lib to import here

logger = logging.getLogger(__name__)
from datetime import datetime, timezone


# This class is a sample. Feel free to customize it.
class SignalStrategy(IStrategy):
    """
    dsfadsdgasfgafgasd
    This is a sample strategy to inspire you.
    More information in https://www.freqtrade.io/en/latest/strategy-customization/

    You can:
        :return: a Dataframe with all mandatory indicators for the strategies
    - Rename the class name (Do not forget to update class_name)
    - Add any methods you want to build your strategy
    - Add any lib you need to build your strategy

    You must keep:
    - the lib in the section "Do not remove these libs"
    - the methods: populate_indicators, populate_entry_trend, populate_exit_trend
    You should keep:
    - timeframe, minimal_roi, stoploss, trailing_*
    """
    # Strategy interface version - allow new iterations of the strategy interface.
    # Check the documentation or the Sample strategy to get the latest version.
    INTERFACE_VERSION = 3

    # Can this strategy go short?
    can_short: bool = True

    # Minimal ROI designed for the strategy.
    # This attribute will be overridden if the config file contains "minimal_roi".
    minimal_roi = {
        "0": 100
    }

    # Optimal stoploss designed for the strategy.
    # This attribute will be overridden if the config file contains "stoploss".
    stoploss = -1.0

    # Trailing stoploss
    trailing_stop = False
    # trailing_only_offset_is_reached = False
    # trailing_stop_positive = 0.01
    # trailing_stop_positive_offset = 0.0  # Disabled / not configured

    # Optimal timeframe for the strategy.
    timeframe = '1m'

    # Run "populate_indicators()" only for new candle.
    process_only_new_candles = True

    # These values can be overridden in the config.
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True

    # Number of candles the strategy requires before producing valid signals
    startup_candle_count: int = 1

    # Optional order type mapping.
    order_types = {
        'entry':                'market',
        'exit':                 'market',
        'stoploss':             'market',
        'takeprofit':           'limit',
        'stoploss_on_exchange': True,
        'takeprofit_on_exchange': True,
    }

    # Optional order time in force.
    order_time_in_force = {
        'entry': 'GTC',
        'exit':  'GTC'
    }

    plot_config = {
        'main_plot': {
            'tema': {},
            'sar':  {'color': 'white'},
        },
        'subplots':  {
            "MACD": {
                'macd':       {'color': 'blue'},
                'macdsignal': {'color': 'orange'},
            },
            "RSI":  {
                'rsi': {'color': 'red'},
            }
        }
    }

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._entry_ts: Optional[TS] = None
        self._force_exit_ts: Optional[TS] = None
        self._entered_trade = False
        self._performed_trades = 0

    def informative_pairs(self):
        """
        Define additional, informative pair/interval combinations to be cached from the exchange.
        These pair/interval combinations are non-tradeable, unless they are part
        of the whitelist as well.
        For more information, please consult the documentation
        :return: List of tuples in the format (pair, interval)
            Sample: return [("ETH/USDT", "5m"),
                            ("BTC/USDT", "15m"),
                            ]
        """
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:


        return dataframe
        # !!!!!!!!!
        # ob = self.dp.orderbook(metadata['pair'], 1)
        #
        # """
        # # first check if dataprovider is available
        # if self.dp:
        #     if self.dp.runmode.value in ('live', 'dry_run'):
        #         ob = self.dp.orderbook(metadata['pair'], 1)
        #         dataframe['best_bid'] = ob['bids'][0][0]
        #         dataframe['best_ask'] = ob['asks'][0][0]
        # """
        #
        # return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        :param dataframe: DataFrame
        :param metadata: Additional information, like the currently traded pair
        :return: DataFrame with entry columns populated
        """

        """
        Send ReduceOnly order after fulfilling the entry order, similar to this
        order = exchange.create_order(symbol, 'market', 'sell', amount, price)
        # place a Reduce Only Stop Loss order on the opened position
        stop_order = exchange.create_order(symbol, 'stop_loss_limit', 'sell', amount, stop_price, {'reduceOnly': True}
        The similar we should do also with closing the position - to use reduceOnly.
        """
        last_price = dataframe[ColumnNames.CLOSE].iloc[-1]
        dataframe[[SignalType.ENTER_LONG, SignalType.ENTER_SHORT, SignalTagType.ENTER_TAG]] = (0, 0, 'simple-strategy-tag')
        tp_price = last_price - 0.0002
        sl_price = last_price + 0.0009
        dataframe[[SignalType.TP_PRICE, SignalType.SL_PRICE]] = (tp_price, sl_price)

        last_agg_ts = TS(dataframe[ColumnNames.DATE].iloc[-1].timestamp())
        current_time = TS.now()
        logging.warning(f"Entry time diff is {float(current_time - last_agg_ts - 60)}")

        if not self._entered_trade and self._performed_trades < 3:
            dataframe.at[dataframe.index[-1], SignalType.ENTER_SHORT] = 1
            self._entry_ts = last_agg_ts
            # assert current_time - (self._entry_ts+60) <= 10, f"entry time is too old: {current_time - self._entry_ts}"
            self._entered_trade = True
            pred_len = 1 * 60
            self._force_exit_ts = last_agg_ts + pred_len
            self._performed_trades += 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        :param dataframe: DataFrame
        :param metadata: Additional information, like the currently traded pair
        :return: DataFrame with exit columns populated
        """
        last_agg_ts = TS(dataframe[ColumnNames.DATE].iloc[-1].timestamp())
        dataframe[['exit_long', 'exit_short', "exit_tag"]] = (0, 0, 'simple-strategy-tag')
        current_time = TS.now()
        logging.warning(f"Exit time diff is {float(current_time - last_agg_ts - 60)}")
        if self._entered_trade and last_agg_ts >= self._force_exit_ts:
            dataframe.at[dataframe.index[-1], 'exit_short'] = 1
            self._entered_trade = False
            self._force_exit_ts = None
        return dataframe

    #Unused yet as found another way to do it
    def get_position_entry_signal(
            self,
            pair: str,
            timeframe: str,
            dataframe: DataFrame,
    ) -> Optional[EntrySignal]:
        """
        Calculates current entry signal based based on the dataframe signals
        columns of the dataframe.
        Used by Bot to get the signal to enter trades.
        :param pair: pair in format ANT/BTC
        :param timeframe: timeframe to use
        :param dataframe: Analyzed dataframe to get signal from.
        :return: (SignalDirection, entry_tag)
        """
        latest, latest_date = self.get_latest_candle(pair, timeframe, dataframe)
        if latest is None or latest_date is None:
            return None

        enter_long = latest[SignalType.ENTER_LONG.value]==1
        exit_long = latest.get(SignalType.EXIT_LONG.value, 0)==1
        enter_short = latest.get(SignalType.ENTER_SHORT.value, 0)==1
        exit_short = latest.get(SignalType.EXIT_SHORT.value, 0)==1
        try:
            TP_price = latest[SignalType.TP_PRICE.value]
        except KeyError:
            TP_price = None
        try:
            SL_price = latest[SignalType.SL_PRICE.value]
        except KeyError:
            SL_price = None

        enter_signal_direction: Optional[SignalDirection] = None
        enter_tag_value: Optional[str] = None
        if enter_long==1 and not any([exit_long, enter_short]):
            enter_signal_direction = SignalDirection.LONG
            enter_tag_value = latest.get(SignalTagType.ENTER_TAG.value, None)
        if (self.config.get('trading_mode', TradingMode.SPOT)!=TradingMode.SPOT
                and self.can_short
                and enter_short==1 and not any([exit_short, enter_long])):
            enter_signal_direction = SignalDirection.SHORT
            enter_tag_value = latest.get(SignalTagType.ENTER_TAG.value, None)

        enter_tag_value = enter_tag_value if isinstance(enter_tag_value, str) else None

        timeframe_seconds = timeframe_to_seconds(timeframe)

        if self.ignore_expired_candle(
                latest_date=latest_date.datetime,
                current_time=datetime.now(timezone.utc),
                timeframe_seconds=timeframe_seconds,
                enter=bool(enter_signal_direction)
        ):
            return None
        entry_signal = EntrySignal(direction=enter_signal_direction, tag=enter_tag_value, TP_price=TP_price, SL_price=SL_price)
        logger.debug(f"entry trigger: {latest['date']} (pair={pair}) "
                     f"enter={enter_long} enter_tag_value={enter_tag_value}")
        return entry_signal
