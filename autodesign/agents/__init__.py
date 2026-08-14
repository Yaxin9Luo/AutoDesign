"""Pre- and post-planner agents — sub-agents that shape the planner's
input/output without joining the tool-use loop.

Currently:
- `PromptEnhancer` (v2.4): expands raw user briefs into structured
  multi-section enhanced briefs before `designer.start`.
- `ClaimGraphExtractor` (v2.8.0): extracts the paper's argumentative arc
  (thesis / tensions / mechanisms / evidence / implications) when a PDF
  is attached. Output feeds the planner (slide arc) and the critic
  (claim_coverage check).
- `DeckOutlineAgent`: decides source-aware deck length and slide outline
  after document ingest, before the designer emits a deck DesignSpec.
- `PaperMemoryAgent`: curates a validated paper-memory dossier from canonical
  paper_memory chunks for designer retrieval.
- `IdentityLogoAgent`: optional coding-agent subprocess that discovers
  official logo candidates before poster planning.
- `CriticAgent` (v2.7.3): forked vision critic with its own LLMBackend,
  own turn budget. Spawned per `critique` tool call
  by the planner; replaces the legacy inline `Critic` class.
- `HyperFramesComposer` (v2.8.1): single-turn LLM agent invoked by the
  `export_video` tool after scaffolding. Writes `index.html` from the
  DESIGN.md + figure manifest, completing the video pipeline end-to-end.
- `ExternalDesignerAuthor`: experimental local coding-agent subprocess that
  authors standalone paper-poster HTML/CSS for direct final promotion.
- `ExternalCodeEditor`: local coding-agent subprocess that revises an existing
  paper-poster HTML artifact in multi-turn chat.
"""

from importlib import import_module


_LAZY_EXPORTS = {
    "ClaimGraphExtractor": (".claim_graph_extractor", "ClaimGraphExtractor"),
    "CriticAgent": (".critic_agent", "CriticAgent"),
    "DeckOutlineAgent": (".deck_outline_agent", "DeckOutlineAgent"),
    "HyperFramesComposer": (".hyperframes_composer", "HyperFramesComposer"),
    "ComposerResult": (".hyperframes_composer", "ComposerResult"),
    "load_composer_system_prompt": (".hyperframes_composer", "load_composer_system_prompt"),
    "IdentityLogoAgent": (".identity_logo_agent", "IdentityLogoAgent"),
    "PaperMemoryAgent": (".paper_memory_agent", "PaperMemoryAgent"),
    "PromptEnhancer": (".prompt_enhancer", "PromptEnhancer"),
    "EnhancerResult": (".prompt_enhancer", "EnhancerResult"),
    "ExternalDesignerAuthor": (".external_designer_author", "ExternalDesignerAuthor"),
    "ExternalLandingAuthor": (".external_landing_author", "ExternalLandingAuthor"),
    "ExternalSlidesAuthor": (".external_slides_author", "ExternalSlidesAuthor"),
    "ExternalVideoAuthor": (".external_video_author", "ExternalVideoAuthor"),
    "ExternalCodeEditor": (".external_code_editor", "ExternalCodeEditor"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


__all__ = [
    "PromptEnhancer", "EnhancerResult",
    "ClaimGraphExtractor",
    "DeckOutlineAgent",
    "IdentityLogoAgent",
    "ExternalDesignerAuthor",
    "ExternalLandingAuthor",
    "ExternalSlidesAuthor",
    "ExternalVideoAuthor",
    "ExternalCodeEditor",
    "PaperMemoryAgent",
    "CriticAgent",
    "HyperFramesComposer", "ComposerResult", "load_composer_system_prompt",
]
