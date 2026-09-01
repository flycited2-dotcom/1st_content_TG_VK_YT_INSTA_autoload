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
    # Каждая тема из справочника должна давать материал — ни счёт, ни лимит
    # не зашиты, чтобы добавление новой темы не роняло тест.
    ideas, _ = load_ideas(KNOWLEDGE)
    drafts = build_editorial_drafts(KNOWLEDGE, set(), len(ideas), tmp_path / "audit.db")

    assert len(drafts) == len(ideas)
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


def _idea(**over):
    from content_factory.agents.editorial import Fact, Idea, Source
    source = Source(id="s1", title="Руководство", publisher="Daikin",
                    url="https://www.daikin.ru/manual", source_type="manufacturer",
                    checked_at="2026-08-01")
    base = dict(id="demo", category="ventilation", content_type="useful",
                title="Заголовок темы", intro="Пояснение проблемы.",
                cta="🔎 Смотреть вентиляцию: https://example.com/go",
                visual="Фотореалистичная сцена в жилой комнате.",
                facts=(Fact("f1", "Первый факт.", source),))
    base.update(over)
    return Idea(**base)


def test_post_opens_with_a_hook_and_offers_a_solution_before_the_link():
    """Пост должен цеплять с первой строки, а не начинаться с сухого заголовка.

    Владелец: «нужны живые посты, как будто их пишет маркетолог» — сначала
    узнаваемая проблема, потом решение, и только затем ссылка.
    """
    idea = _idea(hook="За окном пыль и шум, а форточка — единственный источник воздуха.",
                 offer="Приточная установка ставится за день и работает без открытых окон.")
    text = VkEditorialAgent().write(idea, idea.facts).text
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0] == idea.hook, "первой строкой должен идти крючок"
    assert idea.title not in text, "сухой заголовок темы в тело поста не выводится"
    assert text.index(idea.offer) < text.index(idea.cta), "предложение идёт перед ссылкой"
    assert "Первый факт." in text


def test_topic_without_a_hook_still_builds_the_old_way():
    """Обратная совместимость: тема без крючка не должна ломаться."""
    text = VkEditorialAgent().write(_idea(), _idea().facts).text
    assert text.startswith("Заголовок темы")
