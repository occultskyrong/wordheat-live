"""Strict wire projections. Internal row dictionaries never become HTTP DTOs."""
from typing import Literal,Generic,TypeVar
from pydantic import BaseModel,ConfigDict,Field,model_validator

class Wire(BaseModel): model_config=ConfigDict(extra='forbid',strict=True)
class ErrorBrief(Wire): code:str; message:str; retryable:bool
class SessionRules(Wire):
    duration_seconds:int=Field(ge=30,le=600); intermission_seconds:int=Field(ge=3,le=30); auto_next:bool
    category_id:str|None; difficulty_min:int=Field(ge=1,le=5); difficulty_max:int=Field(ge=1,le=5); strict_round_code:bool
class HintDefinition(Wire): stage:Literal[1,2,3]; text:str
class PublicHint(HintDefinition): revealed_at:str
class NextSelection(Wire):
    selection_id:str|None; version:int; status:Literal['READY','EMPTY','INVALIDATED','NONE']; puzzle_version_id:str|None
    category_id:str|None; difficulty:int|None; answer:str|None; aliases:list[str]; published_hints:list[HintDefinition]; explanation:str|None
    next_rules_version:int; manifest_id:str|None; calibration_id:str|None; selected_at:str|None; reason_code:str|None
class SelectionResult(Wire): selection_id:str; version:int; status:Literal['READY','EMPTY']
class SelectionNotice(SelectionResult): old_selection_id:str|None; reason:str
class CommandProjection(Wire): status:str
class Transport(Wire): state:Literal['DISCONNECTED','CONNECTING','CONNECTED','RECONNECTING','FAULTED']; reason_code:str; recoverable:bool; last_confirmed_at:str|None
class PendingEffect(Wire):
    feedback_id:str; origin_round_id:str; window_epoch:str; delivery_ids:list[str]; outcome:str; status_message:str
    word:str|None; display_name:str; display_score:float|None; solved:bool
class ReadinessContext(Wire):
    session_id:str|None; mode:str|None; run_purpose:str|None; live_profile:str|None; app_id:str|None; room_binding_id:str|None
    installation_id:str; candidate_digest:str; model_manifest_id:str|None; wordbank_manifest_id:str|None; calibration_id:str|None
    evaluated_at:str; authorization_id:str|None
class Blocker(Wire): item:str; status:Literal['pass','fail','pending','not_applicable']; reason_code:str
class Profile(Wire): allowed:bool; blockers:list[Blocker]
class Profiles(Wire): local:Profile; beta:Profile; g2_evidence:Profile; production:Profile
class BackupEntry(Wire):
    backup_id:str|None; status:Literal['none','running','completed','verified','failed']; last_completed_at:str|None; last_verified_at:str|None
    app_version:str|None; schema_version:int|None; model_manifest_id:str|None; location_hint:str|None; last_error:ErrorBrief|None
class BackupStatus(Wire): local:BackupEntry; relay:BackupEntry
class Actor(Wire): actor_id:str; display_name:str; permissions:list[str]
class AuthSession(Wire): actor:Actor; csrf_token:str; expires_at:str
class OperatorSessionRef(Wire): session_id:str; actor_id:str; created_at:str; expires_at:str; current:bool
class OperatorSessions(Wire): sessions:list[OperatorSessionRef]
class Clock(Wire): time_origin:Literal['local_rehearsal','local_replay','relay']; deadline_at:str|None; remaining_ms:int|None=Field(ge=0)
class SessionDetail(Wire):
    session_id:str; version:int; status:str; mode:str; run_purpose:str
    room_binding_id:str|None; session_rules:SessionRules; next_rules:SessionRules|None; active_round_id:str|None
    be_extension_version:str|None=None; next_selection:NextSelection|None=None
class PrivateRound(Wire):
    round_id:str; version:int; status:str; window_epoch:str
    puzzle_version_id:str; answer:str; aliases:list[str]; manifest_id:str; calibration_id:str
    rules_version:int; clock:Clock; pending_command_ids:list[str]
class OfficialIntentState(Wire): intent_id:str; operation:str
class ImportCommandState(Wire):
    validation_id:str; operation:Literal['confirm_valid_rows','cancel']; puzzle_version_ids:list[str]
class CommandResult(Wire):
    command_id:str; status:Literal['pending','applied','failed','cancelled']; resource_version:int
    state:str|SelectionNotice|SelectionResult|CommandProjection|OfficialIntentState|ImportCommandState; error:ErrorBrief|None; be_extension_version:str|None=None
class DisplaySession(Wire):
    display_instance_id:str; render_generation:str; session_id:str; purpose:Literal['primary','preview']; receipt_csrf_token:str; expires_at:str
class TicketResult(Wire): ticket_id:str; ticket:str; expires_at:str
class Feedback(Wire):
    feedback_id:str; word:str|None; display_name:str; status:str; display_score:float|None; solved:bool; message:str; admission_seq:str
class PublicRound(Wire):
    round_id:str; status:str; version:int; window_epoch:str; short_code:str; category:str; answer_length:int
    published_hints:list[PublicHint]; clock:Clock
class PlayerRanking(Wire): display_name:str; points:int; rank:int
class WordRanking(Wire): word:str; first_display_name:str; participant_count:int; display_score:float
class Ranking(Wire): words:list[WordRanking]; players:list[PlayerRanking]
class LatestFeedback(Wire): feedback_id:str; word:str|None; display_name:str; status:str; display_score:float|None; solved:bool; message:str
class Reveal(Wire): round_id:str; answer:str; explanation:str; winner_display_name:str|None; reason:str; next_start_at:str|None
class PublicSnapshot(Wire):
    schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; session_id:str; mode:str; run_purpose:str; as_of_seq:str; server_time:str
    round:PublicRound|None; transport:Transport; ranking:Ranking; latest_feedbacks:list[LatestFeedback]; reveal:Reveal|None; pending_effects:list[PendingEffect]

def validated(model,data): return model.model_validate(data).model_dump(exclude_none=False,exclude_unset=True)
class RoundCreated(Wire):
    round_id:str; short_code:str; status:str; version:int; session_version:int; window_epoch:str; selection_id:str; selection_version:int; be_extension_version:str|None=None; command:CommandResult|None=None
class Check(Wire): status:Literal['pass','fail','pending','not_applicable']; reason_code:str; message:str
class Readiness(Wire):
    checks:dict[str,Check]; gates:dict[str,Check]; can_rehearse:bool; can_test_live:bool; can_go_live:bool
    be_extension_version:str|None=None; context:ReadinessContext|None=None; profiles:Profiles|None=None
    @model_validator(mode='after')
    def complete_items(self):
        if set(self.checks)!={'db','model','wordbank','relay','lease','display'} or set(self.gates)!={'G0','G1_Q','G1_R','G2','rc_verified'}: raise ValueError('Incomplete readiness projection')
        return self
class MaintenanceStatus(Wire):
    command_id:str|None
    operation:Literal['backup_local','verify_local_backup','restore_new_instance','upgrade_app','switch_model']|None
    stage:str
    requires_native_confirmation:bool
    last_error:ErrorBrief|None
class RuntimeStatus(Wire):
    version:int; launcher_state:str; active_session_id:str|None; installation_id:str; app_version:str; model_manifest_id:str|None; schema_version:int
    readiness:Readiness; backup_status:BackupStatus; maintenance_status:MaintenanceStatus

class RehearsalResult(Wire): accepted:bool; reason:str|None; guess_id:str

class Liveness(Wire): status:Literal['alive']
class LoggedOut(Wire): logged_out:bool
class Revoked(Wire): revoked:bool

class StudioSnapshot(Wire):
    schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']; session_id:str; mode:str; run_purpose:str; as_of_seq:str; server_time:str
    public_view:PublicSnapshot; private_round:PrivateRound|None; session_rules:SessionRules; next_rules:SessionRules|None; readiness:Readiness; pending_commands:list[CommandResult]
    be_extension_version:str|None=None; next_selection:NextSelection|None=None

class ReceiptResult(Wire): batch_id:str; stage:Literal['received','processed']; accepted:int=Field(ge=0,le=128)
class MessageSummary(Wire):
    message_id:str; session_id:str; round_id:str|None; source_mode:str; received_at:str; status:str
    reason_code:str|None; normalized_word:str|None; display_name:str
class MessagePage(Wire): items:list[MessageSummary]; next_cursor:str|None
class TraceStep(Wire): stage:str; state:str; time:str; reason_code:str|None
class MessageTrace(Wire):
    message_id:str; source_mode:str; round_id:str|None; stream_id:str|None; stream_seq:str|None
    steps:list[TraceStep]; feedback_refs:list[str]
