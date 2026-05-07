"""
L2 Element Validator

Enforces the rules declared in `l2_element_rules.py` against a Practice's
proposed element list. Called at Practice create/edit time (Batch 4C-i.D
wires it into the routes).

Checks performed
----------------
  • UNKNOWN_FIELD              — element not declared in the L2's spec
                                  (and not a plant-wise extra).
  • MISSING_MANDATORY          — mandatory FieldRule has no provided value.
  • MISSING_CONDITIONAL        — mandatory_if_set: a referenced field is
                                  set but this one is not.
  • INVALID_NUMERIC            — number_2dec / number_4dec value is not a
                                  parseable decimal.
  • INVALID_COSH_REF           — cosh_core: value is not a known active
                                  Cosh entity of the slug's entity_type.
  • CASCADE_VIOLATION          — cosh_cascade: value is not in the cascade
                                  output for the upstream inputs.
  • EMPTY_CASCADE              — cosh_cascade: value provided but cascade
                                  has zero options for the upstream inputs.
  • AUTO_SELECTED_OVERRIDE     — auto_selected field's value disagrees with
                                  the cascade's deterministic output.
  • AUTO_SELECTED_MISSING      — auto_selected field absent while its
                                  cascade-from upstream is fully set.
  • AUTO_SELECTED_UNEXPECTED   — auto_selected field provided while its
                                  cascade-from upstream is NOT fully set.
  • SPECIAL_INPUT_REQUIRED     — ADJUVANTS L2 with practice.is_special_input
                                  False.
  • SPECIAL_INPUT_NOT_ALLOWED  — non-ADJUVANTS L2 with
                                  practice.is_special_input True.
  • FREQUENCY_MISMATCH         — frequency-based L2: Practice.frequency_days
                                  ≠ FERTIGATION_INTERVAL element value.
  • UNKNOWN_L2                 — l2_type not in the rule book.

The validator is db-async because cosh_core: and cosh_cascade: checks
query the Cosh tables via the cascade service. Lookups are memoised
per `validate_l2_elements` call so an L2 with multiple fields
referencing the same Cosh entity_type only hits the DB once.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.l2_element_rules import (
    L2Spec, FieldRule,
    PLANT_WISE_EXTRA_FIELDS, applies_plant_wise_extras,
    COSH_CORE_SLUG_MAP, get_l2_spec,
)
from app.services.cosh_cascade import (
    CascadeOption, list_cascade_options, list_core_options,
)


# ── Public types ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    field_name: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[ValidationError]


# ── Element-shape helpers (work on SQLAlchemy rows OR dicts) ────────────────

def _el_get(el: Any, key: str) -> Any:
    if el is None:
        return None
    if isinstance(el, dict):
        return el.get(key)
    return getattr(el, key, None)


def _provided_value(el: Any) -> Optional[str]:
    """Return the comparable value for an element, prefering cosh_ref
    (entity-backed dropdowns) over value (free text / numeric).
    Returns None if the element is unprovided."""
    if el is None:
        return None
    cr = _el_get(el, "cosh_ref")
    if cr:
        return cr
    v = _el_get(el, "value")
    if v in (None, ""):
        return None
    return v


def _is_provided(el: Any) -> bool:
    return _provided_value(el) is not None


# ── Memoised lookup wrappers ────────────────────────────────────────────────

class _LookupCache:
    """Per-validate-call cache so duplicate cascade/core lookups share
    one DB hit."""

    def __init__(self, db: AsyncSession):
        self._db = db
        self._core: dict[str, list[CascadeOption]] = {}
        self._cascade: dict[tuple[str, tuple[tuple[str, Optional[str]], ...]], list[CascadeOption]] = {}

    async def core(self, entity_type: str) -> list[CascadeOption]:
        if entity_type not in self._core:
            self._core[entity_type] = await list_core_options(self._db, entity_type)
        return self._core[entity_type]

    async def cascade(
        self, name: str, inputs: dict[str, Optional[str]],
    ) -> list[CascadeOption]:
        key = (name, tuple(sorted(inputs.items())))
        if key not in self._cascade:
            self._cascade[key] = await list_cascade_options(self._db, name, inputs)
        return self._cascade[key]


# ── Main validator ──────────────────────────────────────────────────────────

async def validate_l2_elements(
    db: AsyncSession,
    l2_type: str,
    elements: list[Any],
    practice_is_special_input: bool = False,
    practice_frequency_days: Optional[int] = None,
) -> ValidationResult:
    """Validate a Practice's element list against its L2 rule book entry.

    `elements` accepts either SQLAlchemy Element rows or dicts with the
    same field names (element_type, cosh_ref, value)."""
    spec = get_l2_spec(l2_type)
    if spec is None:
        return ValidationResult(
            is_valid=False,
            errors=[ValidationError(
                code="UNKNOWN_L2",
                message=f"Unknown L2 type: {l2_type}",
            )],
        )

    errors: list[ValidationError] = []
    cache = _LookupCache(db)

    # Build element index keyed by element_type
    by_name: dict[str, Any] = {}
    for el in elements:
        et = _el_get(el, "element_type")
        if et:
            by_name[et] = el

    # Allowed fields = spec fields + plant-wise extras when applicable
    allowed_fields: tuple[FieldRule, ...] = spec.fields
    if applies_plant_wise_extras(l2_type):
        allowed_fields = allowed_fields + PLANT_WISE_EXTRA_FIELDS
    allowed_names = {fr.name for fr in allowed_fields}

    # 1. Unknown fields
    for name in by_name:
        if name not in allowed_names:
            errors.append(ValidationError(
                code="UNKNOWN_FIELD",
                field_name=name,
                message=f"{name} is not declared for L2 {l2_type}",
            ))

    # 2. Per-field rules
    for fr in allowed_fields:
        el = by_name.get(fr.name)
        provided = _is_provided(el)
        value = _provided_value(el)

        # Mandatory
        if fr.mandatory and not provided:
            errors.append(ValidationError(
                code="MISSING_MANDATORY",
                field_name=fr.name,
                message=f"{fr.name} is mandatory",
            ))
            continue

        # Conditional mandatory: required when any referenced field is set
        if fr.mandatory_if_set and not provided:
            for ref in fr.mandatory_if_set:
                if _is_provided(by_name.get(ref)):
                    errors.append(ValidationError(
                        code="MISSING_CONDITIONAL",
                        field_name=fr.name,
                        message=f"{fr.name} is required because {ref} is set",
                        details={"because": ref},
                    ))
                    break

        # Auto-selected: presence governed by upstream completeness
        if fr.auto_selected:
            upstream_complete = all(
                _is_provided(by_name.get(ref)) for ref in fr.cascade_from
            )
            if upstream_complete and not provided:
                errors.append(ValidationError(
                    code="AUTO_SELECTED_MISSING",
                    field_name=fr.name,
                    message=f"{fr.name} is auto-selected from {fr.cascade_from} but not present",
                    details={"cascade_from": list(fr.cascade_from)},
                ))
                continue
            if not upstream_complete and provided:
                errors.append(ValidationError(
                    code="AUTO_SELECTED_UNEXPECTED",
                    field_name=fr.name,
                    message=f"{fr.name} provided but upstream {fr.cascade_from} not complete",
                    details={"cascade_from": list(fr.cascade_from)},
                ))
                continue

        if not provided:
            continue

        # Source-specific value validation
        await _validate_value(fr, value, by_name, cache, errors)

    # 3. is_special_input invariant
    if spec.is_special_input and not practice_is_special_input:
        errors.append(ValidationError(
            code="SPECIAL_INPUT_REQUIRED",
            message=f"L2 {l2_type} requires Practice.is_special_input=True",
        ))
    if not spec.is_special_input and practice_is_special_input:
        errors.append(ValidationError(
            code="SPECIAL_INPUT_NOT_ALLOWED",
            message=f"Practice.is_special_input=True is only valid for ADJUVANTS L2",
        ))

    # 4. frequency_based invariant: FERTIGATION_INTERVAL ↔ Practice.frequency_days
    if spec.frequency_based:
        interval_value = _provided_value(by_name.get("FERTIGATION_INTERVAL"))
        if interval_value is not None:
            try:
                interval_int = int(float(interval_value))
            except (TypeError, ValueError):
                interval_int = None
            if interval_int is not None and practice_frequency_days != interval_int:
                errors.append(ValidationError(
                    code="FREQUENCY_MISMATCH",
                    field_name="FERTIGATION_INTERVAL",
                    message=(f"Practice.frequency_days={practice_frequency_days} "
                             f"must equal FERTIGATION_INTERVAL={interval_int}"),
                    details={
                        "practice_frequency_days": practice_frequency_days,
                        "fertigation_interval": interval_int,
                    },
                ))

    return ValidationResult(is_valid=not errors, errors=errors)


# ── Per-source value validation ─────────────────────────────────────────────

async def _validate_value(
    fr: FieldRule,
    value: str,
    by_name: dict[str, Any],
    cache: _LookupCache,
    errors: list[ValidationError],
) -> None:
    src = fr.source

    if src.startswith("cosh_core:"):
        slug = src.split(":", 1)[1]
        entity_type = COSH_CORE_SLUG_MAP[slug]
        options = await cache.core(entity_type)
        if value not in {o.value for o in options}:
            errors.append(ValidationError(
                code="INVALID_COSH_REF",
                field_name=fr.name,
                message=f"{fr.name} value is not an active Cosh {entity_type}",
                details={"value": value, "entity_type": entity_type},
            ))
        return

    if src.startswith("cosh_cascade:"):
        cascade_name = src.split(":", 1)[1]
        inputs = {ref: _provided_value(by_name.get(ref)) for ref in fr.cascade_from}
        options = await cache.cascade(cascade_name, inputs)
        valid_values = {o.value for o in options}

        if not options:
            errors.append(ValidationError(
                code="EMPTY_CASCADE",
                field_name=fr.name,
                message=(f"Cascade {cascade_name} has no options for the "
                         f"upstream selections — value cannot be valid"),
                details={"cascade": cascade_name, "inputs": inputs},
            ))
        elif value not in valid_values:
            errors.append(ValidationError(
                code="CASCADE_VIOLATION",
                field_name=fr.name,
                message=f"{fr.name} value is not in the {cascade_name} cascade output",
                details={"value": value, "cascade": cascade_name, "inputs": inputs},
            ))
        elif fr.auto_selected and len(options) == 1 and value != options[0].value:
            errors.append(ValidationError(
                code="AUTO_SELECTED_OVERRIDE",
                field_name=fr.name,
                message=f"{fr.name} is auto-selected; expected {options[0].value!r}",
                details={"got": value, "expected": options[0].value},
            ))
        return

    if src in ("number_2dec", "number_4dec"):
        try:
            float(value)
        except (TypeError, ValueError):
            errors.append(ValidationError(
                code="INVALID_NUMERIC",
                field_name=fr.name,
                message=f"{fr.name} must be a valid decimal number",
                details={"value": value},
            ))
        return

    # text_box / text_area / hyperlink / media_* / auto_calculated:
    # no validator-side constraint here. Form layer enforces upload
    # mime-types, URL well-formedness, and length limits.
    return


# ── Router-facing assertion + exception ─────────────────────────────────────

class L2ElementValidationError(Exception):
    """Raised by `assert_l2_elements_valid` when the validator returns
    one or more errors. The advisory router maps this to a 422 with the
    full error list so the CA portal can render a single checklist."""

    code = "l2_elements_validation_failed"

    def __init__(self, errors: list[ValidationError]):
        self.errors = errors
        codes = ", ".join(e.code for e in errors)
        super().__init__(
            f"L2 element validation failed: {len(errors)} error(s) ({codes})"
        )


async def assert_l2_elements_valid(
    db: AsyncSession,
    *,
    l2_type: Optional[str],
    elements: list[Any],
    is_special_input: bool = False,
    frequency_days: Optional[int] = None,
) -> None:
    """Convenience wrapper: skips when l2_type is unset, otherwise runs
    the validator and raises L2ElementValidationError on failure.

    Accepts elements as a list of pydantic ElementIn objects, dicts, or
    SQLAlchemy Element rows — duck-typed via `_el_get`."""
    if not l2_type:
        return

    el_dicts: list[dict[str, Any]] = []
    for e in elements:
        if isinstance(e, dict):
            el_dicts.append(e)
        else:
            el_dicts.append({
                "element_type": getattr(e, "element_type", None),
                "cosh_ref": getattr(e, "cosh_ref", None),
                "value": getattr(e, "value", None),
            })

    result = await validate_l2_elements(
        db=db,
        l2_type=l2_type,
        elements=el_dicts,
        practice_is_special_input=is_special_input,
        practice_frequency_days=frequency_days,
    )
    if not result.is_valid:
        raise L2ElementValidationError(result.errors)
