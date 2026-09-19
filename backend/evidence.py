"""Проверка наличия цитат, а не достоверности обещаний поставщика."""
import re
import unicodedata

from .schemas import CompetitorAnalysis, EvidenceCheck, PriceCheck

FACT_LABELS = {
    "assortment": "Ассортимент",
    "origin_countries": "Страны происхождения",
    "farm_names": "Названия ферм",
    "minimum_order": "Минимальный заказ",
    "packaging_and_mixes": "Упаковка и миксы",
    "availability_and_preorder": "Наличие и предзаказ",
    "delivery": "Доставка",
    "payment": "Оплата",
    "claims": "Претензии и брак",
    "ordering": "Оформление заказа",
    "quality_promises": "Обещания качества",
}


class EvidenceMismatch(ValueError):
    pass


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def collect_quotes(analysis: CompetitorAnalysis) -> list[str]:
    quotes = []
    for name in type(analysis.facts).model_fields:
        quotes.extend(getattr(analysis.facts, name).evidence_quotes)
    for item in [*analysis.price_examples, *analysis.claimed_advantages, *analysis.purchase_constraints]:
        quotes.extend(item.evidence_quotes)
    return quotes


def verify_text_evidence(analysis: CompetitorAnalysis, source_text: str) -> EvidenceCheck:
    source = normalized(source_text)
    quotes = collect_quotes(analysis)
    if any(normalized(quote) not in source for quote in quotes):
        # Не возвращаем недостоверную цитату пользователю как подтверждённую.
        raise EvidenceMismatch("A quote was not found in source text")
    return EvidenceCheck(
        mode="text_quotes_matched", quotes_checked=len(quotes),
        note="Цитаты найдены в переданном тексте. Это не подтверждает выполнение обещаний поставщика и правильность всех выводов модели.",
    )


def price_checks(analysis: CompetitorAnalysis) -> list[PriceCheck]:
    result = []
    for index, price in enumerate(analysis.price_examples):
        missing = [
            label for key, label in {
                "variety": "сорт", "stem_length_cm": "длина стебля",
                "stems_per_box": "стеблей в коробке", "currency": "валюта",
                "unit": "единица цены", "valid_date_label": "дата или период цены",
            }.items() if getattr(price, key) is None
        ]
        note = "Сопоставлять только одинаковые товары, длины, упаковки, валюты, единицы и периоды; условия доставки/оплаты проверять отдельно."
        if price.variety and re.search(r"(?i)(имеет.{0,20}аромат|махровый цветок|тонкий аромат)", price.variety):
            missing.append("название сорта требует проверки")
            note += " В поле сорта похоже записано описание цветка; уточните собственное название сорта."
        if price.price_kind == "from":
            note += " Это нижняя граница «от», не фиксированная цена."
        result.append(PriceCheck(example_index=index, missing_parameters=missing, note=note))
    return result


def finalize_text_analysis(analysis: CompetitorAnalysis) -> CompetitorAnalysis:
    """Краткий вывод, ограничения и вопросы строятся из уже извлечённых полей."""
    from textwrap import shorten
    from .schemas import SourceFact

    origin = analysis.facts.origin_countries
    vague_origins = {"со всего мира", "цветы со всего мира", "по всему миру", "весь мир"}
    if origin.value and normalized(origin.value).lower().strip(". ") in vague_origins:
        analysis.facts.origin_countries = SourceFact(status="not_stated", value=None, evidence_quotes=[])

    fragments = []
    for key, label in [("assortment", "Ассортимент"), ("minimum_order", "Минимальный заказ"),
                       ("payment", "Оплата"), ("delivery", "Доставка")]:
        fact = getattr(analysis.facts, key)
        if fact.status == "stated_by_supplier":
            fragments.append(label + ": " + shorten(fact.value, width=130, placeholder="…").rstrip(".") + ".")
        elif fact.status == "conflicting":
            fragments.append(label + ": в материале противоречивые условия.")
    analysis.summary = ("По заявлению поставщика в изученном материале. " + " ".join(fragments)) if fragments else "В изученном материале не извлечены основные условия закупки. Их следует уточнить у поставщика."

    questions = []
    for key, question in [
        ("minimum_order", "Какой минимальный заказ по сумме или количеству?"),
        ("payment", "Какие способы оплаты, размер предоплаты и условия отсрочки?"),
        ("delivery", "Каковы стоимость и условия доставки по Санкт-Петербургу?"),
        ("claims", "Как подать претензию по качеству или количеству и в какой срок?"),
        ("availability_and_preorder", "Что есть в наличии и каковы сроки предзаказа?"),
        ("packaging_and_mixes", "Какая упаковка, сколько стеблей в ней и можно ли смешивать сорта?"),
        ("farm_names", "С каких конкретных ферм поступают выбранные позиции?"),
        ("origin_countries", "Из каких стран поступают выбранные позиции?"),
    ]:
        fact = getattr(analysis.facts, key)
        if fact.status == "not_stated":
            questions.append(question)
        elif fact.status == "conflicting":
            questions.append("Уточните действующее условие по пункту «" + FACT_LABELS[key] + "»: в материале есть противоречие.")
    if any(p.variety is None or p.stem_length_cm is None for p in analysis.price_examples):
        questions.append("Уточните сорта и длины стеблей для выбранных ценовых позиций.")
    if any(p.valid_date_label is None for p in analysis.price_examples):
        questions.append("На какую дату действуют указанные цены и какие условия их применения?")
    analysis.questions_to_supplier = questions[:5]
    analysis.limitations = [
        "Изучен только переданный материал; отсутствие сведений здесь не означает отсутствия услуги у поставщика.",
        "Условия и обещания заявлены поставщиком; их выполнение и актуальность независимо не проверялись.",
        "Наличие цитат не гарантирует правильную интерпретацию: соответствие вывода источнику требует проверки.",
    ]
    if analysis.price_examples:
        analysis.limitations.append("Извлечено до 6 ценовых примеров, а не полный прайс. Сопоставимость и актуальность цен требуют проверки.")
    return CompetitorAnalysis.model_validate(analysis.model_dump())
