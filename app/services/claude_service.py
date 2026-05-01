"""
Claude AI service — crop image analysis + problem descriptions.
Uses claude-sonnet-4-6 for both tasks.

Image analysis: farmer uploads photo → Claude identifies the crop health problem
  + returns 2-sentence farmer-friendly description.
Problem description: given a diagnosed problem → generates 2-sentence plain-language
  summary in the farmer's language.

Both functions degrade gracefully when ANTHROPIC_API_KEY is not set.
"""
import json
import logging
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────────

class ImageAnalysisResult:
    def __init__(
        self,
        problem_name: str,
        problem_cosh_id: Optional[str],
        confidence: str,          # HIGH | MEDIUM | LOW
        description: str,         # 2-sentence farmer-friendly text
        symptoms_observed: list[str],
        raw_response: Optional[str] = None,
    ):
        self.problem_name = problem_name
        self.problem_cosh_id = problem_cosh_id
        self.confidence = confidence
        self.description = description
        self.symptoms_observed = symptoms_observed
        self.raw_response = raw_response

    def to_dict(self) -> dict:
        return {
            "problem_name": self.problem_name,
            "problem_cosh_id": self.problem_cosh_id,
            "confidence": self.confidence,
            "description": self.description,
            "symptoms_observed": self.symptoms_observed,
        }


class ProblemDescriptionResult:
    def __init__(self, description: str, language_code: str):
        self.description = description
        self.language_code = language_code

    def to_dict(self) -> dict:
        return {"description": self.description, "language_code": self.language_code}


# ── Image analysis ─────────────────────────────────────────────────────────────

async def analyze_crop_image(
    image_base64: str,
    media_type: str,                         # "image/jpeg" | "image/png" | "image/webp"
    crop_name: str,
    plant_part_name: str,
    known_problem_ids: Optional[list[str]] = None,
    known_problem_names: Optional[list[str]] = None,
    language_code: str = "en",
) -> ImageAnalysisResult:
    """
    Send farmer's crop photo to Claude for diagnosis.

    Returns the most likely problem + 2-sentence farmer-friendly description.
    If ANTHROPIC_API_KEY is not set, returns a graceful fallback response.

    The prompt constrains Claude to:
    1. Identify the problem visible in the image
    2. Match to a known Cosh problem ID if the list is provided
    3. Write exactly 2 farmer-accessible sentences
    4. Rate confidence honestly
    """
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set — returning placeholder image analysis")
        return ImageAnalysisResult(
            problem_name="Analysis unavailable",
            problem_cosh_id=None,
            confidence="LOW",
            description="Image analysis is not available right now. Please proceed with the guided diagnosis or ask a FarmPundit expert.",
            symptoms_observed=[],
        )

    try:
        import anthropic

        known_problems_text = ""
        if known_problem_ids and known_problem_names:
            problems_list = "\n".join(
                f"- {pid}: {name}"
                for pid, name in zip(known_problem_ids, known_problem_names)
            )
            known_problems_text = f"""
Known problems that commonly affect {crop_name} crops at this stage:
{problems_list}

If you can identify one of these from the image, use its exact ID as problem_cosh_id.
"""

        prompt = f"""You are an expert agricultural diagnostician helping farmers in India identify crop health problems.

A farmer has photographed their {crop_name} crop showing a problem on the {plant_part_name}.

Please analyze this image carefully and:
1. Identify the most likely crop health problem visible
2. Write exactly 2 plain sentences describing what you see — use simple language a farmer with no technical background can understand. No jargon.
3. Rate your confidence as HIGH (very clear), MEDIUM (likely but not certain), or LOW (unclear from image alone)
{known_problems_text}
Respond with ONLY valid JSON, no other text:
{{
  "problem_name": "common name of the problem",
  "problem_cosh_id": "matching_id_from_list_above_or_null",
  "confidence": "HIGH|MEDIUM|LOW",
  "description": "Sentence one about what the farmer will see. Sentence two about what causes this and how serious it is.",
  "symptoms_observed": ["symptom1", "symptom2"]
}}"""

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip()

        # Parse JSON response
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)

        return ImageAnalysisResult(
            problem_name=parsed.get("problem_name", "Unknown problem"),
            problem_cosh_id=parsed.get("problem_cosh_id"),
            confidence=parsed.get("confidence", "LOW"),
            description=parsed.get("description", ""),
            symptoms_observed=parsed.get("symptoms_observed", []),
            raw_response=raw,
        )

    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON for image analysis: {e}")
        return ImageAnalysisResult(
            problem_name="Unrecognised — please try guided diagnosis",
            problem_cosh_id=None,
            confidence="LOW",
            description="The image could not be analysed clearly. Please use the YES/NO questions to identify the problem, or ask a FarmPundit expert.",
            symptoms_observed=[],
        )
    except Exception as e:
        logger.error(f"Claude image analysis failed: {e}")
        return ImageAnalysisResult(
            problem_name="Analysis failed",
            problem_cosh_id=None,
            confidence="LOW",
            description="Image analysis is temporarily unavailable. Please use the guided diagnosis path.",
            symptoms_observed=[],
        )


# ── Problem description ────────────────────────────────────────────────────────

async def describe_crop_problem(
    problem_name: str,
    crop_name: str,
    language_code: str = "en",
    language_name: str = "English",
) -> ProblemDescriptionResult:
    """
    Generate a 2-sentence farmer-friendly description of a diagnosed crop problem.

    Sentence 1: What the farmer will see (symptoms).
    Sentence 2: What causes it and how serious it is.

    Written at farmer literacy level — plain language, no technical jargon.
    Returns in the farmer's selected language.
    """
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set — returning placeholder description")
        return ProblemDescriptionResult(
            description=f"{problem_name} has been identified in your crop.",
            language_code=language_code,
        )

    try:
        import anthropic

        language_instruction = (
            f"Write in {language_name}."
            if language_code != "en"
            else "Write in simple English."
        )

        prompt = f"""You are an agricultural expert writing for farmers in India who may not have formal education.

Write exactly 2 short sentences about {problem_name} affecting {crop_name} crops:
- Sentence 1: Describe what the farmer will see in the field (the visible symptoms)
- Sentence 2: Explain what causes it and how serious it is for the crop

Rules:
- Use words a farmer with Class 5 education would understand
- No Latin names, no chemical names, no technical jargon
- Each sentence should be under 20 words
- {language_instruction}

Output ONLY the 2 sentences, nothing else."""

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )

        description = response.content[0].text.strip()

        return ProblemDescriptionResult(
            description=description,
            language_code=language_code,
        )

    except Exception as e:
        logger.error(f"Claude problem description failed: {e}")
        return ProblemDescriptionResult(
            description=f"{problem_name} has been identified in your {crop_name} crop. Please consult the treatment recommendations below.",
            language_code=language_code,
        )


# ── Batch: enrich problem info with Claude description ─────────────────────────

async def enrich_problem_with_description(
    problem_info: dict,
    crop_name: str,
    language_code: str = "en",
    language_name: str = "English",
) -> dict:
    """Add Claude-generated description to an existing problem_info dict."""
    desc = await describe_crop_problem(
        problem_name=problem_info.get("name", problem_info.get("cosh_id", "")),
        crop_name=crop_name,
        language_code=language_code,
        language_name=language_name,
    )
    return {
        **problem_info,
        "claude_description": desc.description,
        "description_language": language_code,
    }
