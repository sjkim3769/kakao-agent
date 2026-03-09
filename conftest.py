"""
conftest.py - pytest 전역 설정

APScheduler 3.x / aioredis 2.x는 Python 3.12에서 TimeoutError 중복 상속 버그 존재.
테스트 환경에서는 mock으로 대체하여 import 오류 방지.
"""
import sys
from unittest.mock import MagicMock

# Python 3.12 호환성 패치: asyncio.TimeoutError == builtins.TimeoutError 중복 상속 문제
for _mod in [
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
    "aioredis",
    "aioredis.exceptions",
    "aioredis.client",
    "aioredis.connection",
]:
    sys.modules[_mod] = MagicMock()
