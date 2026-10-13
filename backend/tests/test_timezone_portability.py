import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market_data.normalize import normalize_intraday_bars


class ChinaTimezonePortabilityTests(unittest.TestCase):
    def test_intraday_timestamps_use_china_utc_offset_without_system_tzdata(self):
        bars = normalize_intraday_bars([{
            "时间": "2026-10-12 09:30:00", "开盘": 1, "最高": 1.1, "最低": 0.9,
            "收盘": 1.05, "成交量": 1, "成交额": 1,
        }])

        self.assertEqual(bars[0].trade_time.utcoffset().total_seconds(), 8 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
