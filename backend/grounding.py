"""Модель выбирает номера фрагментов; цитаты копирует приложение."""
import copy
import textwrap
from typing import Annotated

from pydantic import Field, create_model

from .evidence import EvidenceMismatch
from .schemas import CitedFinding, CompetitorAnalysis, ImageAnalysis, PriceExample, SourceFact, StrictModel, WholesaleFacts

LineId = Annotated[int, Field(ge=1)]


def draft_schema(public_model, exclude=(), **overrides):
    fields = {}
    for name, info in public_model.model_fields.items():
        if name in exclude:
            continue
        if name == "evidence_quotes":
            copied = copy.deepcopy(info)
            copied.annotation = list[LineId]
            copied.description = "ID фрагментов из source_lines. Не придумывай ID и не переписывай цитаты."
            fields["evidence_line_ids"] = (list[LineId], copied)
        else:
            annotation = overrides.get(name, info.annotation)
            copied = copy.deepcopy(info)
            copied.annotation = annotation
            fields[name] = (annotation, copied)
    # Проверки согласованности публичной модели запускаются после подстановки цитат.
    return create_model(public_model.__name__ + "Draft", __base__=StrictModel, **fields)


ImageAnalysisDraft = draft_schema(ImageAnalysis, exclude=("limitations",))
SourceFactDraft = draft_schema(SourceFact)
PriceExampleDraft = draft_schema(PriceExample)
CitedFindingDraft = draft_schema(CitedFinding)
WholesaleFactsDraft = draft_schema(
    WholesaleFacts, **{name: SourceFactDraft for name in WholesaleFacts.model_fields}
)
CompetitorAnalysisDraft = draft_schema(
    CompetitorAnalysis, exclude=("summary", "questions_to_supplier", "limitations"), facts=WholesaleFactsDraft,
    price_examples=list[PriceExampleDraft], claimed_advantages=list[CitedFindingDraft],
    purchase_constraints=list[CitedFindingDraft],
)


def source_lines(text):
    fragments = []
    for line in text.splitlines():
        fragments.extend(textwrap.wrap(line, width=350, replace_whitespace=False, expand_tabs=False,
                                       break_long_words=True, break_on_hyphens=False))
    return [{"id": index + 1, "text": fragment} for index, fragment in enumerate(fragments)]


def hydrate_evidence(draft, lines):
    lookup = {line["id"]: line["text"] for line in lines}

    def convert(value):
        if isinstance(value, dict):
            result = {key: convert(item) for key, item in value.items() if key != "evidence_line_ids"}
            if "evidence_line_ids" in value:
                ids = value["evidence_line_ids"]
                if any(line_id not in lookup for line_id in ids):
                    raise EvidenceMismatch("Unknown source line ID")
                result["evidence_quotes"] = [lookup[line_id] for line_id in ids]
            return result
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    values = convert(draft.model_dump())
    values.update(summary="Подготовка вывода.", questions_to_supplier=[], limitations=[])
    return CompetitorAnalysis.model_validate(values)
