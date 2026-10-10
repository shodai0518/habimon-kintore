"""今の時刻と 今日の日付。サーバーがどこにあっても 日本時間で数える。テストで変えられるように、ここを通して使う。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import config


def now() -> datetime:
    """今の時刻（日本時間。タイムゾーンの情報は付けない）。"""
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None)


def game_day(moment: datetime) -> date:
    """その時刻が 何日の分か（朝4時で 日付が変わる）。"""
    return (moment - timedelta(hours=config.DAY_START_HOUR)).date()


def today() -> date:
    return game_day(now())


def day_start(day: date) -> datetime:
    return datetime.combine(day, time(config.DAY_START_HOUR))
