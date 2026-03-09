"""
config.py - 환경 설정 관리

[M-8 FIX] .env 파일 실제 연동.
모든 모듈이 이 설정을 import해서 사용해야 함.
하드코딩 설정값 사용 금지.

[품질담당자] 검증:
  - 필수값 누락 시 애플리케이션 시작 즉시 오류 발생 (fail-fast)
  - 시크릿 값은 로그에 절대 출력 안 됨
"""

from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # [품질담당자] extra 필드 금지: 오타로 인한 설정 누락 방지
        extra="forbid",
    )

    # --- DB ---
    database_url: str = Field(..., description="PostgreSQL 연결 URL")
    database_max_connections: int = Field(20, ge=1, le=100)
    database_min_connections: int = Field(5, ge=1)

    # --- Redis ---
    redis_url: str = Field("redis://localhost:6379/0")

    # --- LLM ---
    anthropic_api_key: str = Field(..., description="Anthropic API Key (필수)")
    llm_model: str = Field("claude-sonnet-4-20250514")
    llm_max_tokens: int = Field(1000, ge=100, le=8192)
    llm_timeout_seconds: int = Field(30, ge=5)

    # --- 스케줄러 ---
    collect_cron_hour: int = Field(2, ge=0, le=23)
    collect_cron_minute: int = Field(0, ge=0, le=59)
    agent_cron_hour: int = Field(9, ge=0, le=23)
    timezone: str = Field("Asia/Seoul")

    # --- 데이터 ---
    data_dir: str = Field("/data/kakao_exports")
    log_level: str = Field("INFO")

    # --- 대화방 ---
    room_ids: str = Field("room_001", description="쉼표 구분 대화방 ID 목록")

    # --- 댓글 품질 ---
    comment_auto_approve: bool = Field(True)
    comment_min_length: int = Field(50)
    comment_max_length: int = Field(500)

    # --- 보안 ---
    jwt_secret_key: str = Field(..., description="JWT 서명 시크릿 (필수)")
    jwt_algorithm: str = Field("HS256")
    jwt_expire_minutes: int = Field(1440)

    # --- API ---
    cors_allowed_origins: str = Field("http://localhost:3000", description="쉼표 구분 허용 CORS 도메인")

    debug: bool = Field(False)

    @field_validator("database_min_connections")
    @classmethod
    def min_less_than_max(cls, v, info):
        if "database_max_connections" in info.data and v >= info.data["database_max_connections"]:
            raise ValueError("min_connections는 max_connections보다 작아야 합니다")
        return v

    @property
    def room_id_list(self) -> list[str]:
        """room_ids 문자열을 리스트로 파싱"""
        return [r.strip() for r in self.room_ids.split(",") if r.strip()]

    def __repr__(self) -> str:
        """[품질담당자] 시크릿 마스킹 - 로그 출력 안전"""
        return (
            f"Settings(db=***masked***, redis={self.redis_url}, "
            f"model={self.llm_model}, rooms={self.room_id_list})"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    싱글턴 패턴으로 설정 인스턴스 반환.
    애플리케이션 시작 시 1회만 로드.
    """
    return Settings()
