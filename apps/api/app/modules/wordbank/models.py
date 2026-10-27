"""Private wordbank inputs. Quality and approval fields are server-owned."""
from typing import Literal,Annotated,Union,Generic,TypeVar
from pydantic import BaseModel, ConfigDict, Field, model_validator,field_validator

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Hint(Input):
    stage:Literal[1,2,3]
    text:str=Field(min_length=1,max_length=120)

class Draft(Input):
    word:str
    aliases:list[str]=Field(default_factory=list,max_length=30)
    category_id:str=Field(min_length=1,max_length=80)
    difficulty:int=Field(ge=1,le=5,strict=True)
    hints:list[Hint]=Field(default_factory=list,max_length=3)
    explanation:str=Field(max_length=500)
    source_ref:str=Field(min_length=1,max_length=500)

    @field_validator('word')
    @classmethod
    def normalized_word(cls,value):
        from app.modules.inbox.normalization import normalize_word
        from app.errors import DomainError
        try:return normalize_word(value)
        except DomainError:raise ValueError('Expected one 2–4 Han word') from None

    @model_validator(mode='after')
    def validate_aliases(self):
        from app.modules.inbox.normalization import normalize_word
        from app.errors import DomainError
        try:self.aliases=[normalize_word(value) for value in self.aliases]
        except DomainError:raise ValueError('Invalid aliases') from None
        if len(set(self.aliases))!=len(self.aliases):raise ValueError('Duplicate aliases')
        if len({h.stage for h in self.hints})!=len(self.hints):raise ValueError('Duplicate hint stage')
        return self

class Patch(Draft):
    expected_version:int=Field(ge=1,strict=True)

class Pair(Input):
    guess:str=Field(min_length=1,max_length=32)
    human_rating:Literal['hot','warm','cold','exclude']

class Evaluation(Input):
    puzzle_version_id:str=Field(min_length=1,max_length=128)
    manifest_id:str=Field(min_length=1,max_length=128)
    split:Literal['explore','calibration','blind']
    pairs:list[Pair]=Field(min_length=3,max_length=1000)

    @model_validator(mode='after')
    def unique_pairs(self):
        if len({p.guess for p in self.pairs})!=len(self.pairs): raise ValueError('Duplicate pair')
        return self

class Command(Input):
    command:str
    expected_version:int=Field(ge=0,strict=True)
    args:dict=Field(default_factory=dict)

class EmptyArgs(Input): pass
class ReviewArgs(Input):
    evaluation_id:str=Field(min_length=1,max_length=128)
    decision:Literal["approve","reject"]
    notes:str=Field(max_length=500)
class PublishArgs(Input): approved_version:int=Field(ge=1,strict=True)
class DisableArgs(Input): reason:str=Field(min_length=1,max_length=200)
class EvaluateArgs(Input):
    manifest_id:str=Field(min_length=1,max_length=128)
    split:Literal['explore','calibration','blind']
    pairs:list[Pair]=Field(min_length=3,max_length=1000)
class ApproveCommand(Input): command:Literal['approve']; expected_version:int=Field(ge=1,strict=True); args:ReviewArgs
class DisableCommand(Input): command:Literal['disable']; expected_version:int=Field(ge=1,strict=True); args:DisableArgs
class PublishCommand(Input): command:Literal['publish']; expected_version:int=Field(ge=1,strict=True); args:PublishArgs
class EvaluateCommand(Input): command:Literal['evaluate']; expected_version:int=Field(ge=1,strict=True); args:EvaluateArgs
PuzzleCommand=Annotated[Union[ApproveCommand,DisableCommand,PublishCommand,EvaluateCommand],Field(discriminator='command')]
class ConfirmArgs(Input): rows:list[int]=Field(min_length=1,max_length=10000)
class ConfirmCommand(Input): command:Literal['confirm','confirm_valid_rows']; expected_version:int=Field(ge=1,strict=True); args:ConfirmArgs
class CancelImportCommand(Input): command:Literal['cancel']; expected_version:int=Field(ge=1,strict=True); args:EmptyArgs=Field(default_factory=EmptyArgs)
ImportCommand=Annotated[Union[ConfirmCommand,CancelImportCommand],Field(discriminator='command')]
class ReplayCommand(Input): command:Literal['start','pause','resume','cancel']; expected_version:int=Field(ge=1,strict=True); args:EmptyArgs=Field(default_factory=EmptyArgs)

class Progress(Input): done:int=Field(ge=0); total:int=Field(ge=0)
class Brief(Input): code:str; message:str; retryable:bool
class ImportError(Input): field:str; code:str; message:str
class ImportRow(Input): row_number:int; status:Literal['valid','invalid']; errors:list[ImportError]; draft_preview:Draft|None
class ImportResult(Input): validation_id:str; filename:str; row_count:int; valid_count:int; invalid_count:int; rows:list[ImportRow]; next_cursor:str|None; puzzle_version_ids:list[str]
class EvaluationMetrics(Input): pairwise_accuracy:float|None; total_pairs:int; evaluated_pairs:int; excluded_pairs:int; by_relation:dict[str,float]
class EvaluationPair(Pair): raw_similarity:float|None; display_score:float|None; quality_flag:str
class EvaluationResult(Input):
    manifest_id:str; puzzle_version_id:str; annotation_locked:bool; metrics:EvaluationMetrics; pairs:list[EvaluationPair]; calibration_id:str|None
    quality_scope:Literal['single-puzzle-evaluation']; global_G1_Q:Literal[False]
class ReplayResult(Input): replay_session_id:str; events_total:int; events_applied:int; events_rejected:int; replay_cursor:str
class JobResult(Input):
    job_id:str; status:Literal['queued','running','succeeded','failed','cancelled']; version:int; progress:Progress
    result:ImportResult|EvaluationResult|ReplayResult|None; error:Brief|None; state:str|None=None
T=TypeVar('T')
class Envelope(Input,Generic[T]): data:T; request_id:str; contract_version:Literal['LW-C1/1.0.0']
class PuzzleResult(Draft):
    source_ref:str=Field(max_length=500)
    id:str; puzzle_id:str; version:int; status:Literal['draft','approved','published','disabled']
    manifest_id:str|None; calibration_id:str|None; evaluation_id:str|None; approved_by:str|None
class PuzzlePage(Input):
    items:list[PuzzleResult]; page:int; page_size:int; total:int
    next_cursor:str|None; has_more:bool; as_of:str; expires_at:str
