"""
BL-01 — PoP Guided Elimination Algorithm
Pure function service. No database access. All inputs pre-loaded by the router.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-01
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParameterOption:
    id: str
    name: str
    display_order: int = 0


@dataclass
class VariableOption:
    id: str
    name: str


@dataclass
class PackageStub:
    id: str
    name: str
    description: Optional[str]
    # variables: {parameter_id: variable_id}
    variable_map: dict = field(default_factory=dict)


@dataclass
class EliminationStep:
    done: bool = False
    # When done=True:
    package: Optional[PackageStub] = None
    summary: list[str] = field(default_factory=list)  # plain-language variable names
    # When done=False (question to ask):
    parameter: Optional[ParameterOption] = None
    variables: list[VariableOption] = field(default_factory=list)
    remaining_count: int = 0
    auto_selected: bool = False  # True when this step was auto-resolved
    # Error state:
    error: Optional[str] = None  # "DATA_CONFIG_ERROR"


def run_elimination(
    pool: list[PackageStub],
    parameters: list[ParameterOption],    # MUST be sorted by display_order ascending
    answered: dict[str, str],             # {parameter_id: variable_id}
    variable_names: dict[str, str],       # {variable_id: display_name} for summary
) -> EliminationStep:
    """
    BL-01 core algorithm.
    Returns the next question to ask, or the final package if elimination is complete,
    or an error if pool becomes empty (configuration error).

    Spec steps:
    1. Apply all previously answered parameters to filter the pool.
    2. If pool == 1 → done.
    3. If pool == 0 → DATA_CONFIG_ERROR.
    4. Find next unanswered parameter (in display_order).
    5. Collect only variables present in remaining pool for that parameter.
    6. If 1 variable → auto-select silently, recurse.
    7. Return the question with filtered variables.
    8. If no unanswered parameters remain and pool > 1 → DATA_CONFIG_ERROR.
    """
    # Step 1: apply previous answers
    remaining = _apply_answers(pool, answered)

    # Step 3: empty pool — data config error
    if len(remaining) == 0:
        return EliminationStep(error="DATA_CONFIG_ERROR")

    # Step 2: exactly one package
    if len(remaining) == 1:
        return EliminationStep(
            done=True,
            package=remaining[0],
            summary=_build_summary(answered, variable_names),
        )

    # Steps 4–8: find next parameter to ask
    answered_param_ids = set(answered.keys())

    for param in parameters:
        if param.id in answered_param_ids:
            continue

        # Variables present in at least one package in remaining pool for this parameter
        valid_var_ids = {
            pkg.variable_map[param.id]
            for pkg in remaining
            if param.id in pkg.variable_map
        }

        if not valid_var_ids:
            continue  # this parameter doesn't discriminate — skip it

        valid_variables = [
            VariableOption(id=vid, name=variable_names.get(vid, vid))
            for vid in valid_var_ids
        ]

        # Step 6: single-option auto-selection
        if len(valid_variables) == 1:
            new_answered = {**answered, param.id: valid_variables[0].id}
            result = run_elimination(pool, parameters, new_answered, variable_names)
            result.auto_selected = True
            return result

        # Step 7: return the question
        return EliminationStep(
            done=False,
            parameter=param,
            variables=valid_variables,
            remaining_count=len(remaining),
        )

    # Step 8: no more parameters but pool > 1 → config error
    return EliminationStep(error="DATA_CONFIG_ERROR")


def _apply_answers(pool: list[PackageStub], answered: dict[str, str]) -> list[PackageStub]:
    """Filter pool to only packages matching ALL answered parameters."""
    result = pool
    for param_id, var_id in answered.items():
        result = [
            pkg for pkg in result
            if pkg.variable_map.get(param_id) == var_id
        ]
    return result


def _build_summary(answered: dict[str, str], variable_names: dict[str, str]) -> list[str]:
    """Build the plain-language confirmation summary (variable names only, no parameter labels)."""
    return [variable_names.get(var_id, var_id) for var_id in answered.values()]
