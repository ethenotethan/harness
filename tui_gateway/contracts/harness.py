"""Fork (harness): wire contracts for the Portal-facing RPC surface — wiki, living artifacts,
learning library, news feed, read-only file browser, push registration, per-service architecture
models and service logs/deployments, the cron dataflow graph and its changeset log.

Params are closed (an unknown key answers ``4000`` like every upstream method). Results whose
closed shape is owned by a store or a document contract (``architecture_contract``, the wiki
scanner, ``cron/changesets.py``) type their stable top-level keys and stay ``OpenModel`` so the
producer can extend them without tripping the strict gate.
"""

from __future__ import annotations

from pydantic import Field

from .base import JsonValue, Params, Payload, Result
from .common import OpenModel, SessionParams
from .registry import event, method


# ── session introspection ──────────────────────────────────────────────────────────────────────


class PromptSection(Result):
    name: str
    source: str
    content_preview: str
    full_content: str
    token_count: int
    char_count: int
    color: str


class TokenCount(Result):
    token_count: int
    count: int | None = None
    message_count: int | None = None


class PromptBreakdownResult(Result):
    session_id: str
    model: str
    context_limit: int
    total_system_tokens: int
    sections: list[PromptSection]
    tool_definitions: TokenCount
    conversation_history: TokenCount


method("session.prompt_breakdown", params=SessionParams, result=PromptBreakdownResult,
       doc="The session's system prompt decomposed into sections with token counts.")


class TimelineEvent(OpenModel):
    type: str
    timestamp: float | int | None = None


class SessionTimelineResult(Result):
    session_id: str
    events: list[TimelineEvent]
    total_duration_seconds: float
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None


method("session.timeline", params=SessionParams, result=SessionTimelineResult,
       doc="Temporally structured session events (turns, tool starts/ends) for playback.")


class SetPromptParams(SessionParams):
    prompt: str | None = None


class SetPromptResult(Result):
    prompt: str


method("session.set_prompt", params=SetPromptParams, result=SetPromptResult,
       doc="Set (or clear with an empty string) an ephemeral system prompt append on the live agent.")


class CreditsViewResult(Result):
    logged_in: bool
    balance_lines: list[str]
    identity_line: str | None = None
    topup_url: str | None = None
    depleted: bool


method("credits.view", params=Params, result=CreditsViewResult,
       doc="Structured Nous credit view for the /credits command; fail-open to logged_in=false.")


# ── skills ─────────────────────────────────────────────────────────────────────────────────────


class SkillsGetParams(Params):
    skill_id: str | None = None
    file_path: str | None = None


class SkillFileResult(Result):
    skill: OpenModel
    file_path: str
    content: str
    read_only: bool


method("skills.get", params=SkillsGetParams, result=SkillFileResult,
       doc="Read a locally installed skill's SKILL.md (or one file inside its directory).")


class SkillsUpdateParams(Params):
    skill_id: str | None = None
    content: str | None = None


class SkillsUpdateResult(Result):
    skill: OpenModel


method("skills.update", params=SkillsUpdateParams, result=SkillsUpdateResult,
       doc="Replace a locally installed skill's SKILL.md content and reload skills.")


# ── read-only file browser ─────────────────────────────────────────────────────────────────────


class FilesListParams(Params):
    root: str | None = None
    path: str | None = None


class FilesListResult(OpenModel):
    roots: list[str] | None = None


method("files.list", params=FilesListParams, result=FilesListResult,
       doc="Browsable roots (no root) or one root's bounded, containment-checked tree.")


class FilesReadParams(Params):
    root: str | None = None
    path: str | None = None


method("files.read", params=FilesReadParams, result=OpenModel,
       doc="Read one UTF-8 text file from a browsable root.")


# ── wiki ───────────────────────────────────────────────────────────────────────────────────────


class WikiParams(Params):
    wiki: str | None = None


class WikiScanParams(WikiParams):
    path: str | None = None


class WikiGraphResult(OpenModel):
    pages: list[OpenModel]
    links: list[OpenModel]


method("wiki.scan", params=WikiScanParams, result=WikiGraphResult,
       doc="Scan a wiki directory into pages and typed links.")


class WikiPageParams(WikiParams):
    path: str | None = None


method("wiki.page", params=WikiPageParams, result=OpenModel, doc="One wiki page with frontmatter and body.")


class WikiListResult(Result):
    wikis: list[OpenModel]


method("wiki.list", params=Params, result=WikiListResult, doc="The configured wikis.")

method("wiki.glossary", params=WikiParams, result=OpenModel,
       doc="The validated glossary of one configured wiki (disabled state when absent).")


class WikiGlossaryUpdateParams(WikiParams):
    version: int | None = None
    mode: str | None = None
    proper_nouns: JsonValue | None = None
    if_match: str | None = None


method("wiki.glossary.update", params=WikiGlossaryUpdateParams, result=OpenModel,
       doc="Validate and atomically replace a configured wiki's glossary (409 on a stale if_match).")


class WikiTaxonomyResult(Result):
    taxonomy: JsonValue
    flat_paths: list[str]


method("wiki.taxonomy", params=WikiParams, result=WikiTaxonomyResult,
       doc="The hierarchical taxonomy tree and its flattened paths.")


class WikiExpandLinksParams(WikiParams):
    slug: str | None = None


method("wiki.expand_links", params=WikiExpandLinksParams, result=OpenModel,
       doc="Expand a page's integration links into live status objects.")


class WikiChangesetsParams(WikiParams):
    page: str | None = None
    action: str | None = None
    trigger: str | None = None
    limit: int | None = None
    offset: int | None = None
    since: str | None = None
    until: str | None = None


method("wiki.changesets", params=WikiChangesetsParams, result=OpenModel,
       doc="Paginated wiki changesets (timeline view).")


class WikiEventsParams(WikiParams):
    kind: str | None = None
    limit: int | None = None
    offset: int | None = None
    since: str | None = None
    until: str | None = None


method("wiki.events", params=WikiEventsParams, result=OpenModel,
       doc="The ingestion event log with the changesets each event produced.")


class WikiChangesetDiffParams(WikiParams):
    id: str | None = None


class WikiChangesetDiffResult(OpenModel):
    diff: str | None = None
    changeset: OpenModel | None = None


method("wiki.changeset_diff", params=WikiChangesetDiffParams, result=WikiChangesetDiffResult,
       doc="The unified git diff of one changeset.")


class WikiUpdateParams(WikiParams):
    path: str | None = None
    body: str | None = None
    frontmatter: JsonValue | None = None
    if_match: str | None = None
    force: bool | None = None
    trigger: str | None = None
    source_events: list[str] | None = None
    summary: str | None = None


method("wiki.update", params=WikiUpdateParams, result=OpenModel,
       doc="Full-replace write of one wiki page with optimistic concurrency; records a changeset.")


# ── living artifacts ───────────────────────────────────────────────────────────────────────────


class ArtifactIdParams(Params):
    id: str | None = None


class ArtifactSetParams(ArtifactIdParams):
    kind: str | None = None
    content: str | None = None
    title: str | None = None
    updated_by: str | None = None
    replace: bool | None = None
    actions: list[JsonValue] | None = None
    queries: list[JsonValue] | None = None


class ArtifactResult(Result):
    artifact: OpenModel


method("artifact.set", params=ArtifactSetParams, result=ArtifactResult,
       doc="Upsert a living artifact; appends a revision and emits artifact.changed.")
method("artifact.get", params=ArtifactIdParams, result=ArtifactResult, doc="One artifact with content.")


class ArtifactListResult(Result):
    artifacts: list[OpenModel]


method("artifact.list", params=Params, result=ArtifactListResult, doc="All artifacts without content, newest first.")


class ArtifactDeletedResult(Result):
    deleted: str


method("artifact.delete", params=ArtifactIdParams, result=ArtifactDeletedResult,
       doc="Remove an artifact and its revisions; emits artifact.changed with deleted=true.")


class ArtifactRevisionsResult(Result):
    revisions: list[OpenModel]


method("artifact.revisions", params=ArtifactIdParams, result=ArtifactRevisionsResult,
       doc="Revision metadata for an artifact, newest first.")


class ArtifactRevisionParams(ArtifactIdParams):
    rev: int | None = None


class ArtifactRevisionResult(Result):
    revision: OpenModel


method("artifact.revision", params=ArtifactRevisionParams, result=ArtifactRevisionResult,
       doc="One revision's full content.")


class ArtifactActionInvokeParams(Params):
    artifact_id: str | None = None
    artifact_rev: int | None = None
    binding_id: str | None = None
    entity_ref: str | None = None
    idempotency_key: str | None = None
    user_context: JsonValue | None = None


class ArtifactActionResult(OpenModel):
    status: str | None = None


method("artifact.action.invoke", params=ArtifactActionInvokeParams, result=ArtifactActionResult,
       doc="Invoke a backend intent declared in an artifact's action manifest.")


class ArtifactActionConfirmParams(Params):
    artifact_id: str | None = None
    challenge: str | None = None


method("artifact.action.confirm", params=ArtifactActionConfirmParams, result=ArtifactActionResult,
       doc="Complete a pending destructive intent after native confirmation.")


class ArtifactActionLogParams(Params):
    artifact_id: str | None = None
    binding_id: str | None = None
    entity_ref: str | None = None
    limit: int | None = None


class ArtifactActionLogResult(Result):
    records: list[OpenModel]


method("artifact.action.log", params=ArtifactActionLogParams, result=ArtifactActionLogResult,
       doc="The invocation ledger for an artifact, newest first.")


class ArtifactQueryParams(Params):
    artifact_id: str | None = None
    artifact_rev: int | None = None
    query_id: str | None = None
    params: JsonValue | None = None


class ArtifactQueryInvokeParams(ArtifactQueryParams):
    cursor: JsonValue | None = None


method("artifact.query.invoke", params=ArtifactQueryInvokeParams, result=ArtifactActionResult,
       doc="Run a query the artifact declares, with page-supplied parameters.")
method("artifact.query.subscribe", params=ArtifactQueryParams, result=ArtifactActionResult,
       doc="Follow a declared query; the current result plus a subscription handle.")


class ArtifactQueryUnsubscribeParams(Params):
    subscription: str | None = None


class ArtifactQueryUnsubscribeResult(Result):
    status: str
    removed: bool


method("artifact.query.unsubscribe", params=ArtifactQueryUnsubscribeParams, result=ArtifactQueryUnsubscribeResult,
       doc="Drop a subscription handle from artifact.query.subscribe.")


class QueryHandlerInfo(Result):
    name: str
    params: JsonValue


class ArtifactQueryHandlersResult(Result):
    handlers: list[QueryHandlerInfo]


method("artifact.query.handlers", params=Params, result=ArtifactQueryHandlersResult,
       doc="The registered query handler names and their parameter schemas.")


class ActionsReloadResult(OpenModel):
    status: str | None = None
    error: str | None = None


method("actions.reload", params=Params, result=ActionsReloadResult,
       doc="Reload plugin action handlers from ~/.hermes/plugins/actions/.")


class GatewayRestartResult(Result):
    status: str


method("gateway.restart", params=Params, result=GatewayRestartResult,
       doc="Re-exec the gateway process in place after the response is sent.")


# ── news feed ──────────────────────────────────────────────────────────────────────────────────


class FeedGetParams(Params):
    sources: list[str] | None = None
    since: str | None = None
    limit: int | None = None
    offset: int | None = None


method("feed.get", params=FeedGetParams, result=OpenModel, doc="The curated news feed from digest pipelines.")


class FeedSourcesResult(Result):
    sources: JsonValue
    total: int


method("feed.sources", params=Params, result=FeedSourcesResult, doc="Available feed sources and article counts.")


class FeedPublishParams(Params):
    source: str | None = None
    articles: list[JsonValue] | None = None


class FeedPublishResult(Result):
    total: int


method("feed.publish", params=FeedPublishParams, result=FeedPublishResult,
       doc="Append articles to the news feed store, deduped per source.")


# ── push notifications ─────────────────────────────────────────────────────────────────────────


class PushRegisterParams(Params):
    token: str | None = None
    platform: str | None = None
    device_name: str | None = None
    bundle_id: str | None = None


class PushRegisterResult(Result):
    registered: bool
    apns_configured: bool
    entry: OpenModel


method("push.register", params=PushRegisterParams, result=PushRegisterResult,
       doc="Register an APNs device token for remote push notifications.")


class PushUnregisterParams(Params):
    token: str | None = None


class PushUnregisterResult(Result):
    removed: bool


method("push.unregister", params=PushUnregisterParams, result=PushUnregisterResult,
       doc="Remove an APNs device token.")


# ── learning library ───────────────────────────────────────────────────────────────────────────


class LearningIdParams(Params):
    id: str | None = None


class LearningCourseSetParams(LearningIdParams):
    title: str | None = None
    summary: str | None = None
    source_session_id: str | None = None
    updated_by: str | None = None


class LearningCourseResult(Result):
    course: OpenModel


method("learning.course.set", params=LearningCourseSetParams, result=LearningCourseResult,
       doc="Create or update a course shell (title/summary).")


class LearningCourseGetResult(Result):
    course: OpenModel
    progress: JsonValue | None = None


method("learning.course.get", params=LearningIdParams, result=LearningCourseGetResult,
       doc="One course with the learner's progress.")


class LearningCoursesResult(Result):
    courses: list[OpenModel]


method("learning.course.list", params=Params, result=LearningCoursesResult, doc="All courses.")


class LearningDeletedResult(Result):
    deleted: str | bool


method("learning.course.delete", params=LearningIdParams, result=LearningDeletedResult, doc="Delete a course.")


class LearningModuleSetParams(LearningIdParams):
    course_id: str | None = None
    title: str | None = None
    overview: str | None = None
    position: int | None = None
    updated_by: str | None = None


class LearningRevResult(OpenModel):
    rev: int | None = None


method("learning.module.set", params=LearningModuleSetParams, result=LearningRevResult,
       doc="Upsert one module; an omitted id mints one.")


class LearningModuleDeleteParams(LearningIdParams):
    course_id: str | None = None
    updated_by: str | None = None


method("learning.module.delete", params=LearningModuleDeleteParams, result=LearningRevResult, doc="Delete a module.")


class LearningStepSetParams(LearningIdParams):
    course_id: str | None = None
    module_id: str | None = None
    title: str | None = None
    type: str | None = None
    markdown: str | None = None
    questions: list[JsonValue] | None = None
    append_questions: bool | None = None
    position: int | None = None
    updated_by: str | None = None


method("learning.step.set", params=LearningStepSetParams, result=LearningRevResult,
       doc="Upsert one lesson/quiz step; append_questions extends a quiz without resending it.")


class LearningStepDeleteParams(LearningIdParams):
    course_id: str | None = None
    module_id: str | None = None
    updated_by: str | None = None


method("learning.step.delete", params=LearningStepDeleteParams, result=LearningRevResult, doc="Delete a step.")


class LearningDeckSetParams(LearningIdParams):
    topic: str | None = None
    updated_by: str | None = None


class LearningDeckResult(Result):
    deck: OpenModel


method("learning.deck.set", params=LearningDeckSetParams, result=LearningDeckResult, doc="Create or update a deck.")


class LearningDeckGetResult(Result):
    deck: OpenModel
    srs: JsonValue | None = None


method("learning.deck.get", params=LearningIdParams, result=LearningDeckGetResult,
       doc="One deck with its spaced-repetition state.")


class LearningDecksResult(Result):
    decks: list[OpenModel]


method("learning.deck.list", params=Params, result=LearningDecksResult, doc="All decks.")
method("learning.deck.delete", params=LearningIdParams, result=LearningDeletedResult, doc="Delete a deck.")


class LearningCardSetParams(Params):
    deck_id: str | None = None
    cards: list[JsonValue] | None = None
    updated_by: str | None = None


method("learning.card.set", params=LearningCardSetParams, result=LearningRevResult,
       doc="Batched card upsert by id: one rev bump for N cards.")


class LearningCardDeleteParams(LearningIdParams):
    deck_id: str | None = None
    updated_by: str | None = None


method("learning.card.delete", params=LearningCardDeleteParams, result=LearningRevResult, doc="Delete a card.")


class LearningProgressParams(Params):
    course_id: str | None = None
    step_id: str | None = None
    kind: str | None = None
    score_percent: int | None = None
    at: str | None = None


class LearningProgressResult(Result):
    progress: OpenModel


method("learning.progress.record", params=LearningProgressParams, result=LearningProgressResult,
       doc="Fold one learner progress event (commutative, so racing devices converge).")


class LearningReviewParams(Params):
    deck_id: str | None = None
    card_id: str | None = None
    quality: int | None = None
    reviewed_at: str | None = None
    state: JsonValue | None = None


class LearningReviewResult(Result):
    state: JsonValue
    applied: bool


method("learning.review.record", params=LearningReviewParams, result=LearningReviewResult,
       doc="Fold one SRS review; the server recomputes SM-2.")


class LearningAttemptParams(Params):
    topic: str | None = None
    score: int | None = None
    total: int | None = None
    completed_at: str | None = None
    questions: JsonValue | None = None
    selected: JsonValue | None = None
    course_id: str | None = None
    step_id: str | None = None
    source_session_id: str | None = None


class LearningAttemptResult(Result):
    attempt_id: str


method("learning.attempt.record", params=LearningAttemptParams, result=LearningAttemptResult,
       doc="Append one finished quiz session to the immutable attempt log.")


class LearningAttemptListParams(Params):
    limit: int | None = None


class LearningAttemptsResult(Result):
    attempts: list[OpenModel]


method("learning.attempt.list", params=LearningAttemptListParams, result=LearningAttemptsResult,
       doc="Newest-first quiz attempt history.")


# ── architecture models and services ───────────────────────────────────────────────────────────


class ServiceParams(Params):
    service: str | None = None


class ArchitectureListResult(Result):
    services: list[OpenModel]
    contract: JsonValue


method("architecture.list", params=Params, result=ArchitectureListResult,
       doc="Every manifest-declared service with its source, revision, last check and conformance.")


class ArchitectureDescribeParams(ServiceParams):
    revision: str | None = None


method("architecture.describe", params=ArchitectureDescribeParams, result=OpenModel,
       doc="The hermes.architecture document for one service at its current or a stored revision.")


class ArchitectureCheckResult(Result):
    service: str
    check: OpenModel


method("architecture.check", params=ServiceParams, result=ArchitectureCheckResult,
       doc="Run the service's own --check in its root and record the result.")
method("architecture.history", params=ServiceParams, result=OpenModel,
       doc="Stored revisions with the commits behind them, the deployed revision and check runs.")


class ArchitectureDiffParams(ServiceParams):
    from_revision: str | None = Field(default=None, alias="from")
    to: str | None = None


method("architecture.diff", params=ArchitectureDiffParams, result=OpenModel,
       doc="What changed between two stored revisions of a service's model.")


class ServiceLogsParams(ServiceParams):
    sink: str | None = None
    lines: int | None = None
    cursor: JsonValue | None = None


method("service.logs", params=ServiceLogsParams, result=OpenModel,
       doc="A bounded read of one log sink of a service, with a cursor to continue from.")


class ServiceLogsFollowParams(ServiceLogsParams):
    enabled: bool | None = None


class ServiceLogsFollowResult(Result):
    service: str
    following: bool
    sink: str
    cursor: str | None = None
    stopped: bool | None = None


method("service.logs.follow", params=ServiceLogsFollowParams, result=ServiceLogsFollowResult,
       doc="Start or stop following a sink; appended lines arrive as service.log events.")


class ServiceDeploymentsResult(Result):
    service: str
    deployments: list[OpenModel]


method("service.deployments", params=ServiceParams, result=ServiceDeploymentsResult,
       doc="A manifest's deployments with probes, log sinks and last known health.")


class ServiceDeploymentsProbeParams(ServiceParams):
    deployment: str | None = None


method("service.deployments.probe", params=ServiceDeploymentsProbeParams, result=ServiceDeploymentsResult,
       doc="Run the declared health and revision probes now and return the results.")


# ── cron dataflow graph and changesets ─────────────────────────────────────────────────────────


class GraphResult(OpenModel):
    nodes: list[OpenModel] | None = None
    edges: list[OpenModel] | None = None


method("cron.graph", params=Params, result=GraphResult,
       doc="The cron interflow dataflow graph: crons, sources, artifacts, services and typed edges.")
method("code.graph", params=ServiceParams, result=GraphResult,
       doc="The code knowledge graph of one service built from its declared source files.")


class CronChangesetsParams(Params):
    limit: int | None = None
    offset: int | None = None
    since: str | None = None
    until: str | None = None
    job: str | None = None


method("cron.changesets", params=CronChangesetsParams, result=OpenModel,
       doc="Recorded history of the cron configuration, newest first.")


class CronChangesetDiffParams(Params):
    id: str | None = None


method("cron.changeset_diff", params=CronChangesetDiffParams, result=OpenModel,
       doc="The configurations on either side of one recorded cron change.")


# ── events ─────────────────────────────────────────────────────────────────────────────────────


class ArchitectureChangedPayload(Payload):
    service: str
    revision: str | None = None
    source: str
    reason: str
    status: str | None = None


event("architecture.changed", ArchitectureChangedPayload,
      doc="A service's model gained a revision (snapshot) or a check finished.")


class ArtifactChangedPayload(Payload):
    id: str
    kind: str | None = None
    title: str | None = None
    rev: int | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    deleted: bool | None = None


event("artifact.changed", ArtifactChangedPayload, doc="An artifact was written, acted on or deleted.")


class LearningChangedPayload(Payload):
    entity: str
    id: str | None = None
    rev: int | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    deleted: bool | None = None


event("learning.changed", LearningChangedPayload, doc="A course, deck, progress record or attempt changed.")
