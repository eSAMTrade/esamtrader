import copy
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
from freqtrade.enums.strategy_dataframe_types import ColumnNames
from freqtrade.exceptions import DependencyException
from freqtrade.exchange import timeframe_to_seconds
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.mixins import LoggingMixin
from freqtrade.persistence import PairLocks, init_db, Trade
from freqtrade.plugins.pairlistmanager import PairListManager
from freqtrade.plugins.protectionmanager import ProtectionManager
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from freqtrade.rpc import RPCManager
from freqtrade.rpc.external_message_consumer import ExternalMessageConsumer
from freqtrade.strategy.interface import IStrategy
from freqtrade.templates.signal_strategy import SignalStrategy
from freqtrade.wallets import Wallets
from freqtrade.util.binance_mig import migrate_binance_futures_names
from freqtrade import constants


logger = logging.getLogger(__name__)


class EsamtradeBot(FreqtradeBot):

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

        self.strategy: IStrategy = StrategyResolver.load_strategy(self.config)

        # Check config consistency here since strategies can set certain options
        #validate_config_consistency(config)

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
        # self.strategy.dp = self.dataprovider
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

        if self.trading_mode == TradingMode.FUTURES:

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

    def startup(self) -> None:
        """
        Called on startup and after reloading the bot - triggers notifications and
        performs startup tasks
        """
        migrate_binance_futures_names(self.config)

        self.rpc.startup_messages(self.config, self.pairlists, self.protections)
        # Update older trades with precision and precision mode
        self.startup_backpopulate_precision()
        # if not self.edge:
        #     # Adjust stoploss if it was changed
        #     Trade.stoploss_reinitialization(self.strategy.stoploss)

        # Only update open orders on startup
        # This will update the database after the initial migration
        self.startup_update_open_orders()

    def process(self) -> None:
        """
        Queries the persistence layer for open trades and handles them,
        otherwise a new trade is created.
        :return: True if one or more trades has been created or closed, False otherwise
        """

        # Check whether markets have to be reloaded and reload them when it's needed
        self.exchange.reload_markets()

        self.update_trades_without_assigned_fees()

        # Query trades from persistence layer
        trades: List[Trade] = Trade.get_open_trades()

        self.active_pair_whitelist = self._refresh_active_whitelist(trades)

        # Refreshing candles
        # self.dataprovider.refresh(self.pairlists.create_pair_list(self.active_pair_whitelist),
        #                           self.strategy.gather_informative_pairs())

        # strategy_safe_wrapper(self.strategy.bot_loop_start, supress_error=True)()

        # self.strategy.analyze(self.active_pair_whitelist)

        with self._exit_lock:
            # Check for exchange cancelations, timeouts and user requested replace
            self.manage_open_orders()

        # Protect from collisions with force_exit.
        # Without this, freqtrade may try to recreate stoploss_on_exchange orders
        # while exiting is in process, since telegram messages arrive in an different thread.
        with self._exit_lock:
            trades = Trade.get_open_trades()
            # First process current opened trades (positions)
            self.exit_positions(trades)

        # Check if we need to adjust our current positions before attempting to buy new trades.
        if self.strategy.position_adjustment_enable:
            with self._exit_lock:
                self.process_open_trade_positions()

        # Then looking for buy opportunities
        if self.get_free_open_trades():
            self.enter_positions()
        if self.trading_mode == TradingMode.FUTURES:
            self._schedule.run_pending()
        Trade.commit()
        self.rpc.process_msg_queue(self.dataprovider._msg_queue)
        self.last_process = datetime.now(timezone.utc)

    def enter_positions(self) -> int:
        """
        Tries to execute entry orders for new trades (positions)
        """
        trades_created = 0

        whitelist = copy.deepcopy(self.active_pair_whitelist)
        if not whitelist:
            self.log_once("Active pair whitelist is empty.", logger.info)
            return trades_created
        # Remove pairs for currently opened trades from the whitelist
        for trade in Trade.get_open_trades():
            if trade.pair in whitelist:
                whitelist.remove(trade.pair)
                logger.debug('Ignoring %s in pair whitelist', trade.pair)

        if not whitelist:
            self.log_once("No currency pair in active pair whitelist, "
                          "but checking to exit open trades.", logger.info)
            return trades_created
        if PairLocks.is_global_lock(side='*'):
            # This only checks for total locks (both sides).
            # per-side locks will be evaluated by `is_pair_locked` within create_trade,
            # once the direction for the trade is clear.
            lock = PairLocks.get_pair_longest_lock('*')
            if lock:
                self.log_once(f"Global pairlock active until "
                              f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)}. "
                              f"Not creating new trades, reason: {lock.reason}.", logger.info)
            else:
                self.log_once("Global pairlock active. Not creating new trades.", logger.info)
            return trades_created
        # Create entity and execute trade for each pair from whitelist
        for pair in whitelist:
            if 'XRP' not in pair:
                continue
            try:
                trades_created += self.create_trade(pair)
            except DependencyException as exception:
                logger.warning('Unable to create trade for %s: %s', pair, exception)

        if not trades_created:
            logger.debug("Found no enter signals for whitelisted currencies. Trying again...")

        return trades_created

    def create_trade(self, pair: str) -> bool:
        """
        Check the implemented trading strategy for buy signals.

        If the pair triggers the buy signal a new trade record gets created
        and the buy-order opening the trade gets issued towards the exchange.

        :return: True if a trade has been created.
        """
        logger.debug(f"create_trade for pair {pair}")

        # analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(pair, self.strategy.timeframe)
        # nowtime = analyzed_df.iloc[-1][ColumnNames.DATE] if len(analyzed_df) > 0 else None

        # get_free_open_trades is checked before create_trade is called
        # but it is still used here to prevent opening too many trades within one iteration
        if not self.get_free_open_trades():
            logger.debug(f"Can't open a new trade for {pair}: max number of trades is reached.")
            return False

        signal = self.strategy.esamtrade_signal
        delta_min_dist_in_ticks = constants.MIN_DST_FOR_LMT_ORD_IN_PRC * signal.last_close_agg_price
        delta_max_dist_in_ticks = constants.MAX_DST_FOR_LMT_ORD_IN_PRC * signal.last_close_agg_price
        if signal.TP == signal.SL == 0:
            self.logger.info("No Signal")
            return False
        elif abs(signal.TP) > delta_max_dist_in_ticks or abs(signal.SL) > delta_max_dist_in_ticks:
            self.logger.info("TP or SL beyond max limits.")
            return False
        elif (signal.TP - delta_min_dist_in_ticks) >= 0 >= (signal.SL + delta_min_dist_in_ticks):
            side = SignalDirection.LONG
        elif (signal.TP + delta_min_dist_in_ticks) <= 0 <= (signal.SL - delta_min_dist_in_ticks):
            side = SignalDirection.SHORT
        else:
            self.logger.critical(f"Wrong values for SL: {signal.SL} and TP: {signal.TP}")
            return False
        tp_price = signal.last_close_agg_price * signal.tick_size + signal.TP * signal.tick_size
        sl_price = signal.last_close_agg_price * signal.tick_size + signal.SL * signal.tick_size
        # entry_signal = self.strategy.get_position_entry_signal(pair,self.strategy.timeframe,analyzed_df)
        # (direction, tag) = entry_signal.direction, entry_signal.tag

        if signal:
            # should we use candle_date ?????
            if self.strategy.is_pair_locked(pair, side=side):
                lock = PairLocks.get_pair_longest_lock(pair, side=side)
                if lock:
                    self.log_once(
                        f"Pair {pair} {lock.side} is locked until "
                        f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)} "
                        f"due to {lock.reason}.",
                        logger.info,
                    )
                else:
                    self.log_once(f"Pair {pair} is currently locked.", logger.info)
                return False
            stake_amount = self.wallets.get_trade_stake_amount(pair, self.edge)

            bid_check_dom = self.config.get("entry_pricing", {}).get("check_depth_of_market", {})

            check_dom_enabled = bid_check_dom.get("enabled", False)
            positive_bid_to_ask_delta = bid_check_dom.get("bids_to_ask_delta", 0) > 0
            if (
                check_dom_enabled
                and positive_bid_to_ask_delta
                and not self._check_depth_of_market(pair, bid_check_dom, side=side)
            ):
                return False

            entry_trade = self._execute_entry(
                pair, stake_amount, is_short=(signal == SignalDirection.SHORT)
            )
            if entry_trade is not None:
                if sl_price is not None:
                    sl_placed = self.create_stoploss_order(entry_trade, stop_price=sl_price)
                if tp_price is not None:
                    tp_placed = self.create_takeprofit_order(entry_trade, limit_price=tp_price)
                return True
        else:
            return False

    # def create_trade(self, pair: str) -> bool:
    #     """
    #     Check the implemented trading strategy for buy signals.
    #
    #     If the pair triggers the buy signal a new trade record gets created
    #     and the buy-order opening the trade gets issued towards the exchange.
    #
    #     :return: True if a trade has been created.
    #     """
    #     logger.debug(f"create_trade for pair {pair}")
    #
    #     analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(pair, self.strategy.timeframe)
    #     nowtime = analyzed_df.iloc[-1]['date'] if len(analyzed_df) > 0 else None
    #
    #     # get_free_open_trades is checked before create_trade is called
    #     # but it is still used here to prevent opening too many trades within one iteration
    #     if not self.get_free_open_trades():
    #         logger.debug(f"Can't open a new trade for {pair}: max number of trades is reached.")
    #         return False
    #
    #     # running get_signal on historical data fetched
    #     entry_signal = self.strategy.get_position_entry_signal(
    #         pair,
    #         self.strategy.timeframe,
    #         analyzed_df
    #     )
    #
    #     (direction, tag) = entry_signal.direction, entry_signal.tag
    #
    #     if entry_signal:
    #         if self.strategy.is_pair_locked(pair, candle_date=nowtime, side=direction):
    #             lock = PairLocks.get_pair_longest_lock(pair, nowtime, direction)
    #             if lock:
    #                 self.log_once(f"Pair {pair} {lock.side} is locked until "
    #                               f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)} "
    #                               f"due to {lock.reason}.",
    #                     logger.info)
    #             else:
    #                 self.log_once(f"Pair {pair} is currently locked.", logger.info)
    #             return False
    #         stake_amount = self.wallets.get_trade_stake_amount(pair, self.edge)
    #
    #         bid_check_dom = self.config.get('entry_pricing', {}).get('check_depth_of_market', {})
    #         if ((bid_check_dom.get('enabled', False)) and
    #                 (bid_check_dom.get('bids_to_ask_delta', 0) > 0)):
    #             if self._check_depth_of_market(pair, bid_check_dom, side=direction):
    #                 return self.execute_entry(
    #                     pair,
    #                     stake_amount,
    #                     enter_tag=tag,
    #                     is_short=(direction==SignalDirection.SHORT)
    #                 )
    #             else:
    #                 return False
    #
    #         return self.execute_entry(
    #             pair,
    #             stake_amount,
    #             enter_tag=tag,
    #             is_short=(direction==SignalDirection.SHORT)
    #         )
    #     else:
    #         return False
