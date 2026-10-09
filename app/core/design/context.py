"""What a design turn runs with: the deck, the settings, the providers and the measuring services.

Slide Studio reached for module globals (the project store, the settings cache, the engine bridge,
the preview threads). Here everything a turn touches is handed in once, as a `DesignContext`, so the
same turn runs in a job worker, in parallel Generate and in a test with a scripted provider and
stubbed measurements.

`DesignServices` are the three measurements a saved slide gets:

* `lint(deck, sid)`: the export lint (static, no browser; `app.core.engine_service.readiness`);
* `review(deck, sid)`: the design review (measured in a browser; `design_refs.design_lint`, plus the
  workzone and header-band rules when the deck has a workzone), or None when unavailable;
* `render(deck, sid)`: the slide as a PNG over its layout (`app.core.preview`), for `preview_slide`
  and the critic.

`default_services()` wires the real ones; tests may replace any of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config.ai import AISettings, ai_settings
from app.core.design.deck import DesignDeck
from app.core.llm import factory
from app.core.llm.ports import LLMProvider, ProviderResolver
from app.core.llm.types import ProviderUnavailable

JSON = dict[str, Any]

LintFn = Callable[[DesignDeck, str], JSON]
ReviewFn = Callable[[DesignDeck, str], list[JSON]]
RenderFn = Callable[[DesignDeck, str], bytes]


@dataclass
class DesignServices:
    lint: LintFn
    review: ReviewFn | None = None
    render: RenderFn | None = None


def default_services() -> DesignServices:
    """The real measurements: engine lint, browser design review, browser preview."""
    from app.core import engine_service, preview

    return DesignServices(lint=engine_service.readiness, review=preview.design_review,
                          render=preview.render_slide_png)


@dataclass
class DesignContext:
    deck: DesignDeck
    settings: AISettings = field(default_factory=lambda: ai_settings)
    resolve: ProviderResolver | None = None
    services: DesignServices | None = None
    #: The design model; None means the deck's own, else `LLM_DESIGN_MODEL`.
    model: str | None = None
    #: Checked before every paid round (and before the critic): True stops the turn.
    should_stop: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if self.resolve is None:
            self.resolve = factory.resolver(self.settings)
        if self.services is None:
            self.services = default_services()

    @property
    def design_model(self) -> str:
        chosen = self.model or self.deck.meta().get("model")
        return str(chosen) if isinstance(chosen, str) and chosen else self.settings.llm_design_model

    @property
    def svc(self) -> DesignServices:
        if self.services is None:  # set in __post_init__; only a caller that cleared it lands here
            self.services = default_services()
        return self.services

    def provider(self, model: str) -> LLMProvider:
        if self.resolve is None:
            self.resolve = factory.resolver(self.settings)
        return self.resolve(model)

    def provider_or_none(self, model: str) -> LLMProvider | None:
        try:
            return self.provider(model)
        except ProviderUnavailable:
            return None

    @property
    def brief_on(self) -> bool:
        return bool(self.settings.design_brief_enabled)

    def stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop())
