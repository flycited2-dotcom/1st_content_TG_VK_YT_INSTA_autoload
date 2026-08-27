from pathlib import Path

import pytest

from content_factory.agents.editorial import (
    IdeaAgent,
    ResearchAgent,
    StrictCriticAgent,
    VkEditorialAgent,
    build_editorial_drafts,
    load_ideas,
)


KNOWLEDGE = Path(__file__).parents[1] / "config" / "vk-editorial-sources.yaml"


def test_editorial_pipeline_builds_only_sourced_non_product_posts(tmp_path):
    drafts = build_editorial_drafts(KNOWLEDGE, set(), 20, tmp_path / "audit.db")

    assert len(drafts) == 13
    assert {draft.content_type for draft in drafts} == {
        "useful", "service", "comparison", "trust",
    }
    assert all(draft.source_urls for draft in drafts)
    assert all("Use case: photorealistic-natural" in draft.visual_prompt for draft in drafts)
    assert all("no watermark" in draft.visual_prompt for draft in drafts)
    assert all("Источник:" not in draft.text for draft in drafts)
    assert all(3 <= len(draft.fact_ids) <= 7 for draft in drafts)
    assert all(
        all(f"{number}. " in draft.text for number in range(1, len(draft.fact_ids) + 1))
        for draft in drafts
    )


def test_idea_agent_does_not_repeat_used_topic():
    ideas, _ = load_ideas(KNOWLEDGE)
    selected = IdeaAgent().choose(ideas, {ideas[0].id}, 2)
    assert len(selected) == 2
    assert ideas[0].id not in {idea.id for idea in selected}


def test_research_agent_rejects_untrusted_domain():
    ideas, _ = load_ideas(KNOWLEDGE)
    with pytest.raises(ValueError, match="Недоверенный домен"):
        ResearchAgent({"example.com"}).verify(ideas[0])


def test_critic_blocks_changed_fact():
    ideas, trusted = load_ideas(KNOWLEDGE)
    idea = ideas[0]
    facts = ResearchAgent(trusted).verify(idea)
    draft = VkEditorialAgent().write(idea, facts)
    broken = type(draft)(
        idea_id=draft.idea_id, category=draft.category,
        content_type=draft.content_type,
        text=draft.text.replace(facts[0].text, "Неподтверждённое обещание"),
        fact_ids=draft.fact_ids, source_urls=draft.source_urls,
        visual_prompt=draft.visual_prompt,
    )
    verdict = StrictCriticAgent(trusted).review(idea, facts, broken)
    assert not verdict.ok
    assert any("изменён" in reason for reason in verdict.reasons)
