import getpass
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from queue import Queue
from typing import Callable, Dict, Optional

from pydantic import condecimal, conint, validate_arguments
from tsx import TS

from freqtrade.util.signal import SignalV4


class CurrentSignalGroup:

    def __init__(self, min_same_signals: int, min_signals_diff: int, num_models_in_group: int) -> None:
        self._num_models_in_group = num_models_in_group
        self._signal_group: Dict[str, SignalV4] = {}
        self._current_group_ts_ms: conint(ge=0) = 0
        self._decision_generated: bool = False
        self._min_same_signals: conint(gt=0) = min_same_signals
        self._min_signals_diff: conint(ge=0) = min_signals_diff

    def add_signal_and_decide(self, signal: SignalV4, current_ts: TS) -> Optional[SignalV4]:
        expected_signal_ts_ms = (current_ts.floor(60) - 60).as_ms
        if self._current_group_ts_ms < expected_signal_ts_ms:
            self._decision_generated = False
            self._signal_group = {}
            self._current_group_ts_ms = expected_signal_ts_ms
        if signal.last_agg_ts_ms == expected_signal_ts_ms:
            self._signal_group[signal.signal_provider] = signal

        return self._get_decision()

    def _get_decision(self) -> Optional[SignalV4]:
        if self._decision_generated:
            return None
        buy_signals = [signal for signal in self._signal_group.values() if signal.direction == 1 and signal.confidence == 1]
        sell_signals = [signal for signal in self._signal_group.values() if signal.direction == -1 and signal.confidence == 1]

        num_signals_already_generated = len(self._signal_group.values())
        num_signals_remaining = self._num_models_in_group - num_signals_already_generated

        count_buy_signals = len(buy_signals)
        count_sell_signals = len(sell_signals)
        signals_diff = abs(count_buy_signals - count_sell_signals)

        signal_diff_is_ok = signals_diff >= (self._min_signals_diff + num_signals_remaining)
        if not signal_diff_is_ok:
            return None

        # whether there is the possibility to do buy if all remaining signals arrive as buy
        maybe_do_buy_after_remaining = (count_buy_signals + num_signals_remaining) >= self._min_same_signals
        # whether the current number of buy signals is enough to do buy
        do_buy_before_remaining = count_buy_signals >= self._min_same_signals

        # whether there is the possibility to do sell if all remaining signals arrive as sell
        maybe_do_sell_after_remaining = (count_sell_signals + num_signals_remaining) >= self._min_same_signals
        # whether the current number of sell signals is enough to do sell
        do_sell_before_remaining = count_sell_signals >= self._min_same_signals

        if do_buy_before_remaining and not maybe_do_sell_after_remaining:
            self._decision_generated = True
            return buy_signals[0]
        elif do_sell_before_remaining and not maybe_do_buy_after_remaining:
            self._decision_generated = True
            return sell_signals[0]
        else:
            return None
