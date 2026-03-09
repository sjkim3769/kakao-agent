"""
conftest.py - pytest 전역 설정

APScheduler 3.x는 Python 3.12에서 TimeoutError 중복 상속 버그 존재.
테스트 환경에서는 mock으로 대체하여 import 오류 방지.
aioredis는 redis[asyncio]로 교체하여 Python 3.12 호환성 해결.
"""
import sys
from unittest.mock import MagicMock

# APScheduler Python 3.12 호환성 패치 (3.x 버전 한정 버그)
for _mod in [
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
]:
    sys.modules[_mod] = MagicMock()
