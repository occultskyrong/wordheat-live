"""Executable HTTP/WS projections; no database access or dependency mutations."""
from typing import Generic,TypeVar,Literal
from pydantic import Field,JsonValue,TypeAdapter
from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from app import dto
from app.modules.wordbank.models import Envelope
from app.modules.realtime.protocol import Resume

class ErrorField(dto.Wire): field:str; reason:str
class C1Error(dto.ErrorBrief): details:dict[str,JsonValue]|list[ErrorField]
class C1ErrorEnvelope(dto.Wire): error:C1Error; request_id:str; contract_version:Literal['LW-C1/1.0.0']
C1_ERROR_RESPONSES={status:{'model':C1ErrorEnvelope,'description':'C1 error envelope'} for status in (400,401,403,404,405,409,410,413,415,422,429,500,503,504)}

class AnalyticsCounts(dto.Wire):
    participants_count:int=Field(ge=0); valid_distinct_guesses:int=Field(ge=0); guess_attempts:int=Field(ge=0)
    business_rejected:int=Field(ge=0); system_failed:int=Field(ge=0); nonterminal:int=Field(ge=0)
class MinuteRow(AnalyticsCounts): bucket_start:str; bucket_end:str; partial:bool
class RoundAnalyticsRow(AnalyticsCounts):
    round_id:str; round_ordinal:int=Field(ge=1); status:str; end_reason:str|None; puzzle_version_id:str
    category_id:str; difficulty:int=Field(ge=1,le=5); has_anomaly:bool; anomaly_codes:list[str]; trace_ref:str
    started_at:str|None; ended_at:str|None; running_duration_ms:int|None=Field(ge=0)
    points_net:int; hints_revealed:int=Field(ge=0); completed_normally:bool; included_in_aggregates:bool; exclusion_reason:str|None; winner_display_name:str|None
class AnalyticsContext(dto.Wire): session_id:str; mode:str; run_purpose:str; live_profile:str|None; candidate_digest:str|None
class AnalyticsAsOf(dto.Wire): captured_at:str; local_commit_watermark:str; studio_as_of_seq:str
class AnalyticsCoverage(dto.Wire):
    started_at:str|None; ended_at:str|None; through_at:str; relay_coverage:Literal['complete','partial','unknown']; relay_confirmed_stream_seq:str|None
class Nonterminal(dto.Wire): admission:int=Field(ge=0); score_jobs:int=Field(ge=0); commands:int=Field(ge=0)
class DeliveryPending(dto.Wire): outbox:int=Field(ge=0); display_effects:int=Field(ge=0); ack_outbox:int=Field(ge=0)
class AnalyticsClosure(dto.Wire):
    state:Literal['open','draining','sealed']; sealed_at:str|None; local_nonterminal:Nonterminal; local_delivery_pending:DeliveryPending
    remote_fulfillment_pending:int|None=Field(ge=0); unresolved_gap:bool
class AnalyticsDenominators(dto.Wire):
    valid_guess_units:int=Field(ge=0); participants:int=Field(ge=0); terminal_nonvoid_rounds:int=Field(ge=0); normal_completed_rounds:int=Field(ge=0)
    guess_attempts:int=Field(ge=0); invalid_attempts:int=Field(ge=0); duration_samples:int=Field(ge=0); latency_samples:int=Field(ge=0); hint_eligible_rounds:int=Field(ge=0)
class RoundCounts(dto.Wire): total:int=Field(ge=0); active:int=Field(ge=0); completed:int=Field(ge=0); void:int=Field(ge=0); interrupted:int=Field(ge=0)
class SourcePartition(dto.Wire):
    source_mode:str; provider_test:bool|None; eligible_for_production:bool; messages:int=Field(ge=0); guess_attempts:int=Field(ge=0); valid_guess_units:int=Field(ge=0); excluded_reason:str|None
class AttemptOutcomes(dto.Wire):
    successful:int=Field(ge=0); business_rejected:int=Field(ge=0); system_failed:int=Field(ge=0); nonterminal:int=Field(ge=0); void_excluded:int=Field(ge=0); unassigned_round:int=Field(ge=0)
class MinuteParticipation(dto.Wire): bucket_seconds:Literal[60]; timezone:Literal['UTC']; items:list[MinuteRow]; next_cursor:str|None
class RoundDetails(dto.Wire): items:list[RoundAnalyticsRow]; next_cursor:str|None; total:int=Field(ge=0)
class AnalyticsAggregate(dto.Wire):
    be_extension_version:Literal['LW-BE/1.1.0']; snapshot_id:str; metric_definition_version:Literal['LW-BE-ANALYTICS/1.1.0']
    context:AnalyticsContext; as_of:AnalyticsAsOf; coverage:AnalyticsCoverage; closure:AnalyticsClosure; denominators:AnalyticsDenominators; round_counts:RoundCounts
    participants_count:int=Field(ge=0); valid_distinct_guesses:int=Field(ge=0); guesses_per_participant:float|None=Field(ge=0)
    round_completion_rate:float|None=Field(ge=0,le=1); median_round_duration_ms:float|None=Field(ge=0)
    hint_rate:float|None=Field(ge=0,le=1); invalid_ratio:float|None=Field(ge=0,le=1)
    pipeline_latency_p50_ms:float|None=Field(ge=0); pipeline_latency_p95_ms:float|None=Field(ge=0); pipeline_latency_p99_ms:float|None=Field(ge=0)
    latency_missing_samples:int=Field(ge=0); source_mode_counts:dict[str,int]; source_partitions:list[SourcePartition]
    attempt_outcomes:AttemptOutcomes; points_net:int; final:bool; minute_participation:MinuteParticipation; round_details:RoundDetails; expires_at:str
R=TypeVar('R')
class AnalyticsPage(dto.Wire,Generic[R]):
    be_extension_version:Literal['LW-BE/1.1.0']; snapshot_id:str; items:list[R]; next_cursor:str|None; total:int=Field(ge=0)

def typed_analytics_router(original):
    """Composition adapter preserves frozen endpoints and supplies executable models."""
    result=APIRouter()
    for route in original.routes:
        rounds=route.path.endswith('/analytics/rounds')
        model=Envelope[AnalyticsPage[RoundAnalyticsRow]] if rounds else Envelope[AnalyticsAggregate|AnalyticsPage[MinuteRow]]
        responses={} if rounds else {200:{'description':'Frozen JSON aggregate/minute page or completed CSV download','content':{'text/csv':{'schema':{'type':'string','format':'binary'}}}}}
        result.add_api_route(route.path,route.endpoint,methods=sorted(route.methods),name=route.name,response_model=model,response_model_exclude_unset=True,responses=responses)
    return result

class ReadyFrame(dto.Wire):
    kind:Literal['resume_ready']; schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; session_id:str
    upto_seq:str; server_time:str; render_generation:str|None; pending_effects:list[dto.PendingEffect]
class HeartbeatFrame(dto.Wire):
    kind:Literal['heartbeat']; schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; session_id:str
    server_time:str; latest_seq:str; transport:dto.Transport
class ResyncFrame(dto.Wire):
    kind:Literal['resync_required']; schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; session_id:str
    reason_code:str; min_seq:str; latest_seq:str
class RoundPayload(dto.Wire): round:dto.PublicRound
class RevealPayload(RoundPayload): reveal:dto.Reveal; ranking:dto.Ranking
class IntermissionPayload(RoundPayload): reveal:dto.Reveal
class FeedbackPayload(dto.Wire): feedbacks:list[dto.LatestFeedback]; effects:list[dto.PendingEffect]
class RankingPayload(dto.Wire): ranking:dto.Ranking
class HintPayload(dto.Wire): round_id:str; published_hints:list[dto.PublicHint]
class CommandPayload(dto.Wire): command:dto.CommandResult
class ConnectionPayload(dto.Wire): transport:dto.Transport
class EventFrame(dto.Wire):
    kind:Literal['event']; schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; event_id:str; session_id:str; round_id:str|None
    seq:str; server_time:str; type:str; delivery_ids:list[str]; delivery_mode:Literal['live','replay']
    payload:RoundPayload|RevealPayload|IntermissionPayload|FeedbackPayload|RankingPayload|HintPayload|CommandPayload|ConnectionPayload

def install_protocol_schema(app):
    def build():
        if app.openapi_schema is not None: return app.openapi_schema
        schema=get_openapi(title=app.title,version=app.version,routes=app.routes)
        server=TypeAdapter(ReadyFrame|HeartbeatFrame|ResyncFrame|EventFrame).json_schema()
        server['$id']='urn:wordheat:local-websocket-server:1'
        client=Resume.model_json_schema()
        client['$id']='urn:wordheat:local-websocket-resume:1'
        schema['x-websocket-protocols']={path:{'transport':'websocket','http_operation':False,'client_initial_frame_schema':client,'server_frame_schema':server,'max_frame_bytes':65536,'audience':audience} for path,audience in (('/api/v1/display/ws','DISPLAY'),('/api/v1/studio/ws','STUDIO'))}
        schema['x-contract-version']='LW-C1/1.0.0'; schema['x-source-spec-sha256']='6c582420ea7d80d66cea1178e8629e7358a6a23a88c1d5ea65843a47f821f63b'
        app.openapi_schema=schema
        return schema
    app.openapi=build
