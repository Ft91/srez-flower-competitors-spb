from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .config import CompetitorId

Quote = Annotated[str, Field(min_length=1, max_length=400)]
ShortText = Annotated[str, Field(min_length=1, max_length=400, pattern=r"^[^{}]*$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TextAnalysisRequest(StrictModel):
    competitor_id: CompetitorId | None = None
    text: str = Field(min_length=10, max_length=40000)
    source_name: str = Field(default="Текст пользователя", min_length=1, max_length=200)
    source_url: HttpUrl | None = None
    captured_at: AwareDatetime | None = None


class SourceFact(StrictModel):
    status: Literal["stated_by_supplier", "not_stated", "conflicting"]
    value: str | None = Field(max_length=450)
    evidence_quotes: list[Quote] = Field(max_length=3)

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if self.status == "not_stated":
            if self.value is not None or self.evidence_quotes:
                raise ValueError("not_stated requires null value and empty evidence")
        elif not self.value or not self.evidence_quotes:
            raise ValueError("A supplier statement requires a value and evidence")
        elif self.status == "conflicting" and len(set(self.evidence_quotes)) < 2:
            raise ValueError("A conflict requires two different source excerpts")
        return self


class WholesaleFacts(StrictModel):
    assortment: SourceFact = Field(description="Виды цветов и ассортимент. Рекламные числа — заявления поставщика.")
    origin_countries: SourceFact = Field(description="Заявленные страны происхождения, не страны доставки.")
    farm_names: SourceFact = Field(description="ТОЛЬКО собственные имена/названия конкретных ферм. Число плантаций или страны не являются именами. Нет имён => status=not_stated, value=null, evidence_quotes=[].")
    minimum_order: SourceFact = Field(description="Минимальный заказ: сумма, коробки, пачки или стебли; не смешивать единицы.")
    packaging_and_mixes: SourceFact = Field(description="Пачки, коробки, количество стеблей и возможность микса.")
    availability_and_preorder: SourceFact = Field(description="Наличие, предзаказ, сроки, регулярные поставки; без гарантии актуальности.")
    delivery: SourceFact = Field(description="Условия доставки именно по СПб; другие регионы явно отделять.")
    payment: SourceFact = Field(description="Предоплата, способы оплаты, отсрочка: только прямо указанные условия.")
    claims: SourceFact = Field(description="Претензии по качеству и количеству, сроки, брак, компенсации.")
    ordering: SourceFact = Field(description="Как оформить заказ: форма, менеджер, магазин, мессенджер. Не оценивать работу канала.")
    quality_promises: SourceFact = Field(description="Рекламные обещания качества/стабильности. Это не доказательства исполнения.")


class PriceExample(StrictModel):
    flower: str = Field(min_length=1, max_length=100)
    variety: str | None = Field(max_length=100, description="Только собственное название сорта, например Explorer. Кустовая, махровый цветок, аромат — описания, не имена сортов; тогда null.")
    stem_length_cm: int | None = Field(gt=0, le=300)
    stems_per_box: int | None = Field(gt=0)
    amount: float = Field(gt=0, allow_inf_nan=False)
    currency: str | None = Field(max_length=10, description="RUB/USD/EUR/CNY и др., только если валюта указана.")
    unit: Literal["stem", "bunch", "box"] | None
    price_kind: Literal["exact", "from"]
    valid_date_label: str | None = Field(max_length=100, description="Дата/период цены из источника. Дату скачивания не использовать.")
    conditions: str | None = Field(max_length=300)
    evidence_quotes: list[Quote] = Field(min_length=1, max_length=3)


class CitedFinding(StrictModel):
    text: str = Field(min_length=1, max_length=350)
    evidence_quotes: list[Quote] = Field(min_length=1, max_length=3)


class CompetitorAnalysis(StrictModel):
    summary: str = Field(min_length=1, max_length=700)
    facts: WholesaleFacts
    price_examples: list[PriceExample] = Field(max_length=6, description="До 6 примеров; это не полный прайс и не поиск самой низкой цены.")
    claimed_advantages: list[CitedFinding] = Field(max_length=3)
    purchase_constraints: list[CitedFinding] = Field(max_length=3)
    questions_to_supplier: list[str] = Field(max_length=5)
    limitations: list[str] = Field(max_length=5)


class VisualCriterion(StrictModel):
    score: int | None = Field(ge=0, le=10)
    reason: str = Field(min_length=1, max_length=350)


class ImageAnalysis(StrictModel):
    description: str = Field(min_length=1, max_length=600)
    visible_business_terms: SourceFact
    design_score: int | None = Field(ge=0, le=10)
    design_score_reason: str = Field(min_length=1, max_length=450)
    readability: VisualCriterion
    b2b_offer_clarity: VisualCriterion
    navigation_clarity: VisualCriterion
    contact_visibility: VisualCriterion
    ordering_path_clarity: VisualCriterion
    recommendations: list[ShortText] = Field(max_length=5)
    limitations: list[ShortText] = Field(min_length=1, max_length=5)


class SourceInfo(StrictModel):
    name: str
    url: HttpUrl | None
    captured_at: AwareDatetime | None = None


class PriceCheck(StrictModel):
    example_index: int
    missing_parameters: list[str]
    note: str


class EvidenceCheck(StrictModel):
    mode: Literal["text_quotes_matched", "visual_review_required"]
    quotes_checked: int
    note: str


class AnalysisResponse(BaseModel):
    history_id: str | None = None
    input_notes: list[str] = Field(default_factory=list)
    success: Literal[True] = True
    schema_version: Literal["0.4"] = "0.4"
    input_type: Literal["text", "image"]
    source: SourceInfo
    model: str
    evidence_check: EvidenceCheck
    price_checks: list[PriceCheck] = Field(default_factory=list)
    analysis: CompetitorAnalysis | ImageAnalysis
