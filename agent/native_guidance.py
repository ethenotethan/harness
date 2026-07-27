"""Fork-specific native-primitive guidance for the system prompt.

The researchoors fork adds native primitives that the HermesNative app
renders and interacts with — the **artifacts registry**, the **LLM wiki**,
and the **news-feed / digest**. Upstream Hermes has no concept of any of
them, so a freshly-forked agent has no idea these capabilities exist or how
to drive them from the native app.

This module holds one guidance block per native primitive. Each block is
injected into the system prompt only when its capability is actually present
(a tool in ``agent.valid_tool_names``, or a wiki on disk), by
``agent.system_prompt.build_system_prompt_parts``. Keeping the blocks in a
single fork-owned file — rather than scattering them through the upstream
``agent/prompt_builder.py`` — minimizes rebase conflicts when the fork
tracks upstream main.

Extension contract
------------------
Every new native primitive lands with four things kept in lockstep so that
"pull the fork onto yourself, then restart" always yields an agent that
knows what it can now do:

1. the RPC / agent tool that exposes the behavior,
2. a capability name in ``gateway.capabilities`` (server.py) so native
   clients can feature-gate,
3. a guidance constant here plus its gate in ``build_system_prompt_parts``,
4. a line in the user-facing docs (``docs/plugins/actions.md`` and kin).

Add the guidance block and its gate in the same PR as the tool — otherwise
the capability ships dark and the agent won't use it until someone notices.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# ── Artifacts registry ────────────────────────────────────────────────────
# Gated on the ``artifact`` tool. Covers both the living-artifact model
# (read-before-write, revisioned kinds) and the action-intents overlay
# (buttons that run server-side handlers). See docs/plugins/actions.md.

ARTIFACT_GUIDANCE = (
    "Living artifacts (the HermesNative artifacts registry): the `artifact` "
    "tool reads and maintains named, revisioned models the native app renders "
    "live — maps, charts, graphs, tables, datasets, timelines, and "
    "self-contained HTML documents. The store is shared across chat turns, "
    "cron jobs, and the app, so ALWAYS read-before-write: `get` the current "
    "content, modify it, then `set` it back — never overwrite from a "
    "hallucinated prior state. Each `set` creates a new revision (`revisions` "
    "shows the audit trail); server-side merge is per-kind and tombstone-aware "
    "for dataset/model/map kinds.\n"
    "\n"
    "Action intents — artifact buttons that do real work. An artifact can "
    "declare buttons that invoke server-side handlers when the user clicks "
    "them in the app. Declarations are stored ALONGSIDE the content, not "
    "inside it: pass the `actions` parameter of the artifact tool's `set` "
    "action (a JSON array). Do NOT embed them in the content body, wrap the "
    "content in a JSON envelope, or edit the artifact index on disk — for "
    "html kind the content stays raw HTML.\n"
    '  artifact set … actions=\'[{"type": "intent", "id": "delete-ticket",\n'
    '    "label": "Delete", "intent": "linear.issue.delete",\n'
    '    "presentation": {"role": "destructive"}}]\'   // omit role if non-destructive\n'
    "In an HTML-kind artifact, wire the click target with inert attributes:\n"
    '  <button data-hermes-binding="delete-ticket" data-hermes-entity="ENG-101">…</button>\n'
    "`data-hermes-binding` names the declaration's `id`; `data-hermes-entity` "
    "is the row's key-field value. The gateway resolves the handler from the "
    "declared `intent` name; a destructive role forces a native confirmation "
    "dialog that leads with the trusted intent name before the handler runs.\n"
    "\n"
    "An intent name must resolve to a REGISTERED handler or the click "
    "returns `unsupported`. Available today: the built-ins "
    "(`artifact.refresh`, `artifact.entity.tombstone`) and any Tier-1 plugin "
    "handlers in ~/.hermes/plugins/actions/*.py — deterministic Python for "
    "anything writable in advance (API calls, DB writes, deletes). There is "
    "NO agent-prompt intent yet (routing a button back through an agent turn "
    "is specced but gated) — do not declare intents like `agent.prompt` "
    "expecting them to work; write a Tier-1 plugin instead. Security rule "
    "for handlers: treat `entity_ref` as a lookup key into the pinned "
    "artifact content and read external IDs from the stored row — never call "
    "an external API with the raw client string.\n"
    "\n"
    "Handlers live in plugin files under ~/.hermes/plugins/actions/. After you "
    "author or edit one, call the `actions.reload` RPC (or ask to reload "
    "actions) — no gateway restart needed. You author the declaration (data); "
    "only filesystem-authored plugins register handlers (behavior), so an "
    "artifact can never smuggle executable code. Full authoring guide: "
    "docs/plugins/actions.md."
)


# ── LLM wiki ───────────────────────────────────────────────────────────────
# Gated on a wiki existing on disk (resolved the same way the gateway does).
# The native app renders the wiki as a graph + page detail + a timeline fed
# by changesets; edits only surface there if they go through the wiki code
# path and capture a changeset.

WIKI_GUIDANCE = (
    "LLM wiki (the knowledge graph the HermesNative app renders): the user "
    "keeps an interlinked markdown wiki that the app shows as a graph view "
    "(`wiki.scan`), page detail (`wiki.page`), a taxonomy filter, and a "
    "Timeline tab (`wiki.changesets`). Pages are markdown with YAML "
    "frontmatter and `[[wikilinks]]`; the graph edges ARE those wikilinks, so "
    "linking pages is what makes the graph connected. Taxonomy comes from the "
    "subdirectories (entities/, concepts/, comparisons/, queries/, projects/, "
    "goals/, life/, issues/, …) and the frontmatter tag path.\n"
    "\n"
    "The app's views only reflect your work if changes go through the native "
    "wiki code path AND you capture a changeset after every write — the "
    "Timeline stays blank if you edit files with raw filesystem tools and skip "
    "the capture. When the user runs the native app and asks you to ingest a "
    "source, file a query, lint the wiki, or asks 'what changed?' / 'show the "
    "timeline', prefer the `llm-wiki-native` skill, which drives that path and "
    "records changesets so the graph and timeline stay live."
)


# ── News feed / digest ──────────────────────────────────────────────────────
# Gated on the ``feed_publish`` tool. The native app renders a news feed read
# from feed.get; feed_publish is the write path.

FEED_GUIDANCE = (
    "News feed (the HermesNative digest surface): the app renders a news feed "
    "backed by the gateway's feed store. The `feed_publish` tool is the write "
    "path — push curated articles into a named source and they appear in the "
    "feed, shown as a filter tab. Articles are deduped per source, so a "
    "recurring producer only lands genuinely new items. The typical producer "
    "is the news-digest cron blueprint; publish under a stable `source` name "
    "(e.g. \"ai-digest\") so dedup and the feed's tab grouping work."
)


# ── Presence detection ──────────────────────────────────────────────────────


def wiki_present() -> bool:
    """True if a wiki exists on disk where the gateway would resolve one.

    Resolved the same way ``tui_gateway.wiki_api.resolve_wiki(None)`` does —
    a ``default`` entry in ~/.hermes/wikis.yaml, else ``$WIKI_PATH``, else
    ~/wiki. Import stays local so a fork that drops wiki support (or an
    upstream sync that removes the module) degrades to "no wiki" rather than
    breaking prompt assembly.
    """
    try:
        from tui_gateway.wiki_api import resolve_wiki

        path = resolve_wiki(None)
    except Exception:
        path = os.environ.get("WIKI_PATH", "") or os.path.expanduser("~/wiki")
    try:
        return bool(path) and Path(path).is_dir()
    except OSError:
        return False


def native_guidance_blocks(
    valid_tool_names, wiki_is_present: Optional[bool] = None
) -> list[str]:
    """Return the native-primitive guidance blocks that apply to this agent.

    Each block is gated on its capability being present: the ``artifact`` and
    ``feed_publish`` tools by name, and the wiki by on-disk presence. Pass
    ``wiki_is_present`` to avoid a filesystem probe (callers that already know,
    and tests); it defaults to :func:`wiki_present`.
    """
    names = valid_tool_names or set()
    blocks: list[str] = []
    if "artifact" in names:
        blocks.append(ARTIFACT_GUIDANCE)
    if wiki_is_present is None:
        wiki_is_present = wiki_present()
    if wiki_is_present:
        blocks.append(WIKI_GUIDANCE)
    if "feed_publish" in names:
        blocks.append(FEED_GUIDANCE)
    return blocks
