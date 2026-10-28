import datetime as dt
from zoneinfo import ZoneInfo
from . import config as C


def now_local():
    return dt.datetime.now(ZoneInfo(C.BOT_TZ))


def day_start_ts():
    return now_local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def today():
    return now_local().date().isoformat()
