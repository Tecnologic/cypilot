# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from cypilot.core.sim_control import MAX_ADVANCE, SimStatus, chunks


def test_advance_is_split_into_writes_of_at_most_one_second() -> None:
    assert chunks(0) == []
    assert chunks(1600) == [1600]
    assert chunks(MAX_ADVANCE) == [MAX_ADVANCE]
    assert chunks(40000) == [MAX_ADVANCE, MAX_ADVANCE, 8000]
    with pytest.raises(ValueError):
        chunks(-1)


def test_virtual_time() -> None:
    assert SimStatus(paused=True, cycle_count=16000, cycle_period_ns=62500).virtual_time == pytest.approx(1.0)
