from pathlib import Path

import pytest
import yaml

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
    # Типографские карточки (цифра, миф, опрос) не рисуются генератором: у них
    # вместо фотопромпта короткое описание надписи.
    ideas_by_id = {idea.id: idea for idea in ideas}
    photos = [d for d in drafts if not ideas_by_id[d.idea_id].card_big]
    cards = [d for d in drafts if ideas_by_id[d.idea_id].card_big]
    # Сейчас у всех тем фото: тёмные карточки владелец забраковал. Механизм карточки
    # остаётся и проверяется отдельно, поэтому пустой список карточек допустим.
    assert photos
    assert all("Use case: photorealistic-natural" in draft.visual_prompt for draft in photos)
    assert all("no watermark" in draft.visual_prompt for draft in photos)
    assert all(draft.visual_prompt.startswith("Типографская карточка") for draft in cards)
    assert all("Источник:" not in draft.text for draft in drafts)
    checklists = [draft for draft in drafts if draft.format == "checklist"]
    assert checklists
    assert all(3 <= len(draft.fact_ids) <= 7 for draft in checklists)
    assert all(
        all(f"{number}. " in draft.text for number in range(1, len(draft.fact_ids) + 1))
        for draft in checklists
    )
    # Рубрики — свободный текст с опорой на факты, без обязательного списка.
    assert {draft.format for draft in drafts} >= {"qa", "myth", "number", "season", "poll"}


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


def test_every_declared_source_is_actually_cited():
    """Осиротевший источник — признак того, что он больше ничем не держится.

    Каталог Daichi 2026 начал отдавать 404, и это обнаружилось только при
    ручной проверке ссылок. Тест ловит обратный случай: источник объявлен,
    но ни один факт на него не ссылается — значит, его пора убрать.
    """
    raw = yaml.safe_load(KNOWLEDGE.read_text(encoding="utf-8"))
    declared = set(raw["sources"])
    cited = {fact["source"] for topic in raw["topics"] for fact in topic["facts"]}

    assert declared == cited, f"источники без фактов: {sorted(declared - cited)}"


def test_post_keeps_at_most_four_facts_each_in_its_own_paragraph(tmp_path):
    """Владелец смотрел живую ленту: список шёл сплошняком и читался стеной.

    Ограничение в четыре пункта держит призыв с ссылкой выше сгиба VK, а
    пустая строка между пунктами делает список сканируемым на телефоне.
    """
    drafts = build_editorial_drafts(KNOWLEDGE, set(), 50, tmp_path / "audit.db")
    assert drafts

    for draft in (d for d in drafts if d.format == "checklist"):
        assert len(draft.fact_ids) <= 4, f"{draft.idea_id}: пунктов больше четырёх"
        body = draft.text
        for index in range(2, len(draft.fact_ids) + 1):
            assert f"\n\n{index}. " in body, (
                f"{draft.idea_id}: пункт {index} не отделён пустой строкой"
            )


# ── Рубрики: живые форматы вместо одного шаблона «крючок → чек-лист» ─────────────

RUBRIC_BODY = (
    "— Кондиционер журчит, как чайник. Он сломался?\n\n"
    "Нет. Производитель объясняет это перетеканием жидкости в контуре.\n\n"
    "Слышите другой звук? Напишите в комментариях."
)


def _rubric(**over):
    base = dict(format="qa", body=RUBRIC_BODY)
    base.update(over)
    return _idea(**base)


def test_rubric_post_is_the_authored_body_without_a_checklist():
    idea = _rubric()
    text = VkEditorialAgent().write(idea, idea.facts).text

    assert text == RUBRIC_BODY
    assert "Что важно проверить" not in text and "1. " not in text


def test_rubric_draft_carries_its_format_for_rotation():
    idea = _rubric(format="myth")
    assert VkEditorialAgent().write(idea, idea.facts).format == "myth"


def test_critic_accepts_rubric_whose_numbers_come_from_facts():
    from content_factory.agents.editorial import Fact
    idea = _rubric(
        body="Ночью установка шумит не более 20 дБ(А). Слышно ли её у вас?",
        facts=(Fact("f1", "В ночном режиме шум не более 20 дБ(А).",
                    _idea().facts[0].source),),
    )
    facts = ResearchAgent({"daikin.ru"}).verify(idea)
    draft = VkEditorialAgent().write(idea, facts)

    assert StrictCriticAgent({"daikin.ru"}).review(idea, facts, draft).ok


def test_critic_blocks_rubric_number_that_no_fact_supports():
    """Свободный текст — главный риск выдумки: цифра без факта не проходит."""
    idea = _rubric(body="Установка шумит не более 15 дБ(А). А у вас что слышно?")
    facts = ResearchAgent({"daikin.ru"}).verify(idea)
    draft = VkEditorialAgent().write(idea, facts)

    verdict = StrictCriticAgent({"daikin.ru"}).review(idea, facts, draft)

    assert not verdict.ok
    assert any("15" in reason for reason in verdict.reasons)


def test_idea_agent_offers_in_season_topics_first_and_skips_out_of_season():
    winter = _idea(id="winter", months=(12, 1, 2))
    autumn = _idea(id="autumn", months=(9, 10))
    always = _idea(id="always")

    picked = IdeaAgent().choose([winter, always, autumn], set(), 5, month=10)

    assert [idea.id for idea in picked] == ["autumn", "always"]


def test_rotation_does_not_put_the_same_format_twice_in_a_row():
    from content_factory.orchestrator.vk_content_plan import rotate_editorial_items

    class Item:
        def __init__(self, name, category, fmt):
            self.name, self.category, self.format = name, category, fmt

    items = [Item("a", "ups", "qa"), Item("b", "ups", "qa"), Item("c", "ventilation", "qa"),
             Item("d", "ups", "myth"), Item("e", "stabilizers", "number")]

    order = rotate_editorial_items(items, previous_category="", previous_format="qa")

    assert order[0].format != "qa"
    # Пока в пуле есть другой формат, подряд одинаковые не встают.
    for index in range(len(order) - 1):
        rest = {item.format for item in order[index + 1:]}
        if rest - {order[index].format}:
            assert order[index + 1].format != order[index].format


def test_text_card_is_rendered_as_a_square_png(tmp_path):
    from PIL import Image
    from content_factory.content.text_card import render_text_card

    path = render_text_card("20 дБ(А)", "Ночной шум приточки ZEAV 135", tmp_path / "card.png")

    with Image.open(path) as image:
        assert image.size == (1080, 1080)


def test_rubrics_take_slots_before_old_checklists():
    """Чек-листы стоят в справочнике первыми, но лента не должна быть ими забита.

    Владелец: «одни и те же сообщения, аж противно». При нехватке слотов
    предпочтение отдаётся рубрикам, а чек-листы добираются по остатку.
    """
    checklist = _idea(id="old-1")
    rubric = _rubric(id="new-1", format="myth")
    seasonal = _rubric(id="season-1", format="season", months=(10,))

    picked = IdeaAgent().choose([checklist, rubric, seasonal], set(), 2, month=10)

    assert [idea.id for idea in picked] == ["season-1", "new-1"]


def test_season_is_checked_against_every_month_of_the_planning_horizon():
    """30 сентября горизонт в 14 дней уже целиком в октябре.

    Фильтр по месяцу «сегодня» выкинул октябрьские темы из плана именно в
    последний день сентября: сезонные посты выходили бы с опозданием.
    """
    october = _rubric(id="october", format="season", months=(10, 11))
    winter = _rubric(id="winter", format="season", months=(12, 1))

    picked = IdeaAgent().choose([october, winter], set(), 5, month=(9, 10))

    assert [idea.id for idea in picked] == ["october"]


def test_long_question_wraps_inside_the_margins(tmp_path):
    """Вопрос из чата — фраза, а не цифра: она переносится, а не уезжает за край."""
    from PIL import Image
    from content_factory.content.text_card import ACCENT, SIZE, render_text_card

    path = render_text_card("Из рекуператора капает вода", "Брак или так и должно быть?",
                            tmp_path / "q.png", kicker="Вопрос из чата")

    with Image.open(path) as image:
        assert image.size == (SIZE, SIZE)
        pixels = image.load()
        right_margin = [pixels[x, y] for x in range(SIZE - 70, SIZE) for y in range(0, SIZE, 4)]
        assert ACCENT not in right_margin, "крупный текст вылез за правое поле"
