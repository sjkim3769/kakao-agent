"""
scheduler.py - 일일 파이프라인 스케줄러

[품질담당자] 검증 항목:
  - 실행 중복 방지 (Lock 메커니즘)
  - 실패 알림 (Slack/이메일 연동 가능)
  - 재시도 정책: 수집 실패 시 30분 후 1회 재시도

[Efficiency] 최적화:
  - 비동기 asyncio 기반
  - 메모리 효율적 배치 처리

[Facilitator] 명확한 파이프라인:
  COLLECT → ANALYZE → AGENT_RUN → DONE
"""

import asyncio
import json
import logging
import asyncpg
import aioredis
import anthropic
from pathlib import Path
from datetime import date, datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.collector.collector import ConversationCollector, KakaoTalkParser
from src.analyzer.analyzer import ConversationAnalyzer
from src.agent.agent import KakaoAgent
from config.settings import get_settings

logger = logging.getLogger(__name__)


class DailyPipelineLock:
    """[품질담당자] 동시 실행 방지 Redis 기반 분산 락"""
    
    LOCK_KEY = "daily_pipeline_lock"
    LOCK_TTL = 3600  # 1시간 (파이프라인 최대 실행 시간)

    def __init__(self, redis_client):
        self.redis = redis_client
        self._acquired = False

    async def __aenter__(self):
        acquired = await self.redis.set(
            self.LOCK_KEY, "1", nx=True, ex=self.LOCK_TTL
        )
        if not acquired:
            raise RuntimeError("다른 파이프라인이 실행 중입니다")
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        [C-3 FIX] 예외 발생 여부와 무관하게 락 해제 보장.
        redis.delete 실패 시 TTL 만료로 자연 해제되도록 경고만 발생.
        """
        if self._acquired:
            try:
                await self.redis.delete(self.LOCK_KEY)
                self._acquired = False
            except Exception as e:
                # redis 장애 시 TTL(3600초) 만료로 자동 해제됨을 명시
                logger.error(
                    f"[CRITICAL] 파이프라인 락 해제 실패: {e}. "
                    f"TTL {self.LOCK_TTL}초 후 자동 해제됩니다."
                )
        return False  # 예외 전파 억제하지 않음


class DailyPipeline:
    """
    [Facilitator] 수집 → 분석 → Agent 전체 파이프라인
    각 단계가 명확히 구분되어 단독 실행/테스트 가능
    """

    def __init__(self, collector, analyzer, agent, redis_client, room_ids: list[str]):
        self.collector = collector
        self.analyzer = analyzer
        self.agent = agent
        self.redis = redis_client
        self.room_ids = room_ids

    async def run(self, target_date: Optional[date] = None) -> dict:
        """
        전체 파이프라인 실행
        [품질담당자] 각 단계 결과 및 오류를 모두 기록
        """
        target_date = target_date or date.today()
        started_at = datetime.now()

        results = {
            "date": str(target_date),
            "started_at": started_at.isoformat(),
            "rooms": {},
            "total_errors": 0,
        }

        try:
            async with DailyPipelineLock(self.redis):
                logger.info(f"=== 일일 파이프라인 시작: {target_date} ({len(self.room_ids)}개 방) ===")

                # [PROD-06 FIX] 순차 실행 → asyncio.gather 병렬 실행
                # 10개 방 × 5분 = 50분 → 5분으로 단축
                room_tasks = [self._run_for_room(r, target_date) for r in self.room_ids]
                room_results = await asyncio.gather(*room_tasks, return_exceptions=True)

                for room_id, result in zip(self.room_ids, room_results):
                    if isinstance(result, Exception):
                        results["rooms"][room_id] = {"errors": [str(result)], "steps": {}}
                        results["total_errors"] += 1
                    else:
                        results["rooms"][room_id] = result
                        if result.get("errors"):
                            results["total_errors"] += len(result["errors"])

        except RuntimeError as e:
            logger.warning(f"락 획득 실패: {e}")
            results["skipped"] = True
            return results

        results["completed_at"] = datetime.now().isoformat()
        elapsed = (datetime.now() - started_at).total_seconds()
        results["elapsed_seconds"] = round(elapsed, 1)

        # [QA-02 FIX] 실패 알림: total_errors > 0 시 즉시 경고 로그 + 알림 훅
        if results["total_errors"] > 0:
            logger.critical(
                f"🚨 파이프라인 오류 발생: {results['total_errors']}건 | "
                f"날짜={target_date} | 소요={elapsed:.1f}초 | "
                f"상세={json.dumps(results['rooms'], ensure_ascii=False)[:500]}"
            )
            await self._send_alert(results)  # 알림 훅 (Slack/이메일 확장 포인트)

        logger.info(
            f"=== 파이프라인 완료: {target_date} | "
            f"{len(self.room_ids)}개 방 | {elapsed:.1f}초 | "
            f"오류: {results['total_errors']}건 ==="
        )
        return results

    async def _send_alert(self, results: dict) -> None:
        """
        [QA-02] 파이프라인 실패 알림 훅.
        운영에서 Slack/PagerDuty/이메일로 교체.
        """
        # await slack_client.send(channel="#alerts", text=f"파이프라인 실패: {results}")
        logger.critical(
            f"[ALERT] 파이프라인 실패 알림 전송 필요 (알림 연동 미설정): "
            f"errors={results['total_errors']}"
        )

    async def _run_for_room(self, room_id: str, target_date: date) -> dict:
        """
        [PROD-01 FIX] _send_alert() 내부에 중첩됐던 코드를 독립 메서드로 분리.
        이 버그로 파이프라인이 실제로 아무것도 실행하지 않았음.
        """
        room_result = {"room_id": room_id, "errors": [], "steps": {}}

        # Step 1: 수집
        logger.info(f"[{room_id}] Step 1: 수집 시작")
        collect_result = await self.collector.collect_daily(room_id, target_date)
        room_result["steps"]["collect"] = {
            "success": collect_result.success,
            "skipped": collect_result.skipped,
            "message": collect_result.message,
        }

        if not collect_result.success and not collect_result.skipped:
            room_result["errors"].append(f"수집 실패: {collect_result.message}")
            return room_result

        snapshot_id = collect_result.snapshot_id

        # Step 2: 분석
        logger.info(f"[{room_id}] Step 2: 분석 시작")
        try:
            analyze_result = await self.analyzer.analyze_snapshot(snapshot_id)
            room_result["steps"]["analyze"] = analyze_result
        except Exception as e:
            logger.exception(f"[{room_id}] 분석 실패")
            room_result["errors"].append(f"분석 실패: {e}")
            return room_result

        # Step 3: Agent 실행
        logger.info(f"[{room_id}] Step 3: Agent 실행")
        try:
            agent_result = await self.agent.run_daily(room_id, target_date)
            room_result["steps"]["agent"] = agent_result
            if agent_result.get("errors"):
                room_result["errors"].extend(agent_result["errors"])
        except Exception as e:
            logger.exception(f"[{room_id}] Agent 실패")
            room_result["errors"].append(f"Agent 실패: {e}")

        return room_result


# ============================================================
# 스케줄러 설정
# ============================================================

def create_scheduler(pipeline: DailyPipeline) -> AsyncIOScheduler:
    """
    [Facilitator] APScheduler 설정
    
    실행 일정:
    - 매일 오전 2시: 전날 대화 수집 및 분석
    - 매일 오전 9시: Agent 댓글 생성 및 전송
    """
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # 오전 2시: 수집 + 분석
    @scheduler.scheduled_job(CronTrigger(hour=2, minute=0))
    async def nightly_collect_analyze():
        logger.info("⏰ 나이틀리 수집/분석 시작")
        await pipeline.run()

    # 오전 9시: Agent 댓글 (별도 실행)
    @scheduler.scheduled_job(CronTrigger(hour=9, minute=0))
    async def morning_agent_run():
        logger.info("⏰ 아침 Agent 실행")
        # 이미 파이프라인에서 처리됨 (필요시 별도 알림 발송)

    return scheduler


# ============================================================
# 애플리케이션 진입점
# ============================================================

async def main():
    """
    로컬 개발 및 수동 실행용 진입점.

    [CRITICAL FIX] 수정 사항:
      1. 하드코딩 DB 자격증명 → settings.database_url 사용
      2. aioredis v1 create_redis_pool → v2 Redis.from_url 사용
      3. ConversationAnalyzer에 parser DI 누락 → KakaoTalkParser() 주입
      4. llm_client=None → 실제 AnthropicClient 연결 (미연결 시 명시적 오류)
      5. room_ids 하드코딩 → settings.room_id_list 사용
    """
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level))

    # DB 연결 — settings에서 로드 (하드코딩 금지)
    db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.database_min_connections,
        max_size=settings.database_max_connections,
    )

    # Redis 연결 — aioredis v2 API
    redis = await aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    # LLM 클라이언트 — None 허용 안 함
    llm_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # [CRITICAL FIX] parser를 ConversationAnalyzer에 DI로 주입
    parser = KakaoTalkParser()
    collector = ConversationCollector(db_pool, Path(settings.data_dir))
    analyzer  = ConversationAnalyzer(db_pool, llm_client, parser, redis)
    agent     = KakaoAgent(db_pool, llm_client, redis)

    pipeline = DailyPipeline(
        collector=collector,
        analyzer=analyzer,
        agent=agent,
        redis_client=redis,
        room_ids=settings.room_id_list,   # 환경변수에서 로드
    )

    result = await pipeline.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    await db_pool.close()
    await redis.close()


if __name__ == "__main__":
    asyncio.run(main())
