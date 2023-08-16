import logging
from datetime import datetime, time, timezone
from threading import Lock
from typing import List, Union

from schedule import Scheduler

from freqtrade import constants
from freqtrade.configuration import validate_config_consistency
from freqtrade.constants import Config
from freqtrade.data.dataprovider import DataProvider
from freqtrade.edge import Edge
from freqtrade.enums import SignalDirection, State, TradingMode
from freqtrade.exchange import timeframe_to_seconds
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.mixins import LoggingMixin
from freqtrade.persistence import PairLocks, init_db
from freqtrade.plugins.pairlistmanager import PairListManager
from freqtrade.plugins.protectionmanager import ProtectionManager
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from freqtrade.rpc import RPCManager
from freqtrade.rpc.external_message_consumer import ExternalMessageConsumer
from freqtrade.strategy.interface import IStrategy
from freqtrade.templates.signal_strategy import SignalStrategy
from freqtrade.wallets import Wallets


logger = logging.getLogger(__name__)


class EsamtradeSignalBot(FreqtradeBot):

    def __init__(self, config: Config) -> None:
        """
        Init all variables and objects the bot needs to work
        :param config: configuration dict, you can use Configuration.get_config()
        to get the config dict.
        """
        self.active_pair_whitelist: List[str] = []

        # Init bot state
        self.state = State.STOPPED

        # Init objects
        self.config = config

        self.strategy: Union[IStrategy, SignalStrategy] = StrategyResolver.load_strategy(self.config)

        # Check config consistency here since strategies can set certain options
        validate_config_consistency(config)

        self.exchange = ExchangeResolver.load_exchange(
            self.config['exchange']['name'], self.config, load_leverage_tiers=True)

        init_db(self.config['db_url'])

        self.wallets = Wallets(self.config, self.exchange)

        PairLocks.timeframe = self.config['timeframe']

        self.pairlists = PairListManager(self.exchange, self.config)

        # RPC runs in separate threads, can start handling external commands just after
        # initialization, even before Freqtradebot has a chance to start its throttling,
        # so anything in the Freqtradebot instance should be ready (initialized), including
        # the initial state of the bot.
        # Keep this at the end of this initialization method.
        self.rpc: RPCManager = RPCManager(self)

        self.dataprovider = DataProvider(self.config, self.exchange, rpc=self.rpc)
        self.pairlists = PairListManager(self.exchange, self.config, self.dataprovider)

        self.dataprovider.add_pairlisthandler(self.pairlists)

        # Attach Dataprovider to strategy instance
        self.strategy.dp = self.dataprovider
        # Attach Wallets to strategy instance
        self.strategy.wallets = self.wallets

        # Initializing Edge only if enabled
        self.edge = Edge(self.config, self.exchange, self.strategy) if \
            self.config.get('edge', {}).get('enabled', False) else None

        # Init ExternalMessageConsumer if enabled
        self.emc = ExternalMessageConsumer(self.config, self.dataprovider) if \
            self.config.get('external_message_consumer', {}).get('enabled', False) else None

        self.active_pair_whitelist = self._refresh_active_whitelist()

        # Set initial bot state from config
        initial_state = self.config.get('initial_state')
        self.state = State[initial_state.upper()] if initial_state else State.STOPPED

        # Protect exit-logic from forcesell and vice versa
        self._exit_lock = Lock()
        LoggingMixin.__init__(self, logger, timeframe_to_seconds(self.strategy.timeframe))

        self.trading_mode: TradingMode = self.config.get('trading_mode', TradingMode.SPOT)

        self._schedule = Scheduler()

        if self.trading_mode==TradingMode.FUTURES:

            def update():
                self.update_funding_fees()
                self.wallets.update()

            # TODO: This would be more efficient if scheduled in utc time, and performed at each
            # TODO: funding interval, specified by funding_fee_times on the exchange classes
            for time_slot in range(0, 24):
                for minutes in [0, 15, 30, 45]:
                    t = str(time(time_slot, minutes, 2))
                    self._schedule.every().day.at(t).do(update)
        self.last_process = datetime(1970, 1, 1, tzinfo=timezone.utc)

        self.strategy.ft_bot_start()
        # Initialize protections AFTER bot start - otherwise parameters are not loaded.
        self.protections = ProtectionManager(self.config, self.strategy.protections)

    def create_trade(self, pair: str) -> bool:
        """
        Check the implemented trading strategy for buy signals.

        If the pair triggers the buy signal a new trade record gets created
        and the buy-order opening the trade gets issued towards the exchange.

        :return: True if a trade has been created.
        """
        logger.debug(f"create_trade for pair {pair}")

        analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(pair, self.strategy.timeframe)
        nowtime = analyzed_df.iloc[-1]['date'] if len(analyzed_df) > 0 else None

        # get_free_open_trades is checked before create_trade is called
        # but it is still used here to prevent opening too many trades within one iteration
        if not self.get_free_open_trades():
            logger.debug(f"Can't open a new trade for {pair}: max number of trades is reached.")
            return False

        # running get_signal on historical data fetched
        entry_signal = self.strategy.get_position_entry_signal(
            pair,
            self.strategy.timeframe,
            analyzed_df
        )

        (direction, tag) = entry_signal.direction, entry_signal.tag

        if entry_signal:
            if self.strategy.is_pair_locked(pair, candle_date=nowtime, side=direction):
                lock = PairLocks.get_pair_longest_lock(pair, nowtime, direction)
                if lock:
                    self.log_once(f"Pair {pair} {lock.side} is locked until "
                                  f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)} "
                                  f"due to {lock.reason}.",
                        logger.info)
                else:
                    self.log_once(f"Pair {pair} is currently locked.", logger.info)
                return False
            stake_amount = self.wallets.get_trade_stake_amount(pair, self.edge)

            bid_check_dom = self.config.get('entry_pricing', {}).get('check_depth_of_market', {})
            if ((bid_check_dom.get('enabled', False)) and
                    (bid_check_dom.get('bids_to_ask_delta', 0) > 0)):
                if self._check_depth_of_market(pair, bid_check_dom, side=direction):
                    return self.execute_entry(
                        pair,
                        stake_amount,
                        enter_tag=tag,
                        is_short=(direction==SignalDirection.SHORT)
                    )
                else:
                    return False

            return self.execute_entry(
                pair,
                stake_amount,
                enter_tag=tag,
                is_short=(direction==SignalDirection.SHORT)
            )
        else:
            return False
