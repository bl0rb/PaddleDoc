from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


_QUALITY_THRESHOLD_A = 0.9
_QUALITY_THRESHOLD_B = 0.75
_SCORE_KEY_HINTS = (
    'confidence',
    'conf',
    'score',
    'scores',
    'dt_score',
    'dt_scores',
    'rec_score',
    'det_score',
    'prob',
    'probability',
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _looks_like_score_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(hint in lowered for hint in _SCORE_KEY_HINTS)


def _normalise_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
        if candidate > 1.0 and candidate <= 100.0:
            candidate /= 100.0
        if 0.0 <= candidate <= 1.0:
            return candidate
    return None


def _collect_numeric_values(payload: Any) -> list[float]:
    values: list[float] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _looks_like_score_key(key):
                values.extend(_collect_numeric_values(value))
            else:
                values.extend(_collect_numeric_values(value))
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            values.extend(_collect_numeric_values(item))
    else:
        normalised = _normalise_score(payload)
        if normalised is not None:
            values.append(normalised)
    return values


def _as_block_list(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = page.get('parsing_res_list')
    if isinstance(blocks, list):
        return [block for block in blocks if isinstance(block, dict)]
    return []


def ocr_confidence_score(raw_outputs: list[dict[str, Any]] | None) -> float:
    if not raw_outputs:
        return 0.0

    confidences: list[float] = []
    for payload in raw_outputs:
        confidences.extend(_collect_numeric_values(payload))

    if not confidences:
        return 0.0

    mean_conf = sum(confidences) / len(confidences)
    low_conf_ratio = sum(confidence < 0.8 for confidence in confidences) / len(confidences)
    return _clamp(mean_conf * (1 - low_conf_ratio))


def structure_quality_score(page_structures: list[dict[str, Any]] | None, block_stats: dict[str, Any] | None = None) -> float:
    if not page_structures:
        return 0.0

    page_count = len(page_structures)
    if isinstance(block_stats, dict):
        block_page_count = block_stats.get('page_count')
        if isinstance(block_page_count, int) and block_page_count > 0:
            page_count = block_page_count

    pages_with_blocks = 0
    total_blocks = 0
    ordered_blocks = 0
    table_blocks = 0
    table_blocks_with_content = 0

    for page in page_structures:
        blocks = _as_block_list(page)
        if blocks:
            pages_with_blocks += 1
        for block in blocks:
            total_blocks += 1
            if block.get('block_order') is not None:
                ordered_blocks += 1
            label = str(block.get('block_label') or '').lower()
            content = str(block.get('block_content') or '').strip()
            if 'table' in label:
                table_blocks += 1
                if content:
                    table_blocks_with_content += 1

    page_coverage = pages_with_blocks / page_count if page_count else 0.0
    order_quality = ordered_blocks / total_blocks if total_blocks else 0.0
    table_quality = 1.0 if table_blocks == 0 else table_blocks_with_content / table_blocks

    return _clamp((0.5 * page_coverage) + (0.3 * order_quality) + (0.2 * table_quality))


def text_noise_penalty(markdown: str) -> float:
    cleaned = markdown.strip()
    if not cleaned:
        return 1.0

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    tokens = _TOKEN_RE.findall(cleaned)
    unique_tokens = {token.lower() for token in tokens}
    repetition_ratio = 1 - (len(unique_tokens) / len(tokens)) if tokens else 0.0
    line_repetition_ratio = 1 - (len(set(lines)) / len(lines)) if lines else 0.0
    symbol_ratio = sum(1 for character in cleaned if not character.isalnum() and not character.isspace()) / len(cleaned)
    gibberish_ratio = sum(1 for token in tokens if _looks_gibberish(token)) / len(tokens) if tokens else 0.0

    penalty = (0.35 * repetition_ratio) + (0.25 * line_repetition_ratio) + (0.2 * symbol_ratio) + (0.2 * gibberish_ratio)
    return _clamp(penalty)


def _looks_gibberish(token: str) -> bool:
    lowered = token.lower()
    letters = [character for character in lowered if character.isalpha()]
    if not letters:
        return len(token) >= 6

    vowel_count = sum(character in 'aeiou' for character in letters)
    if len(letters) >= 7 and vowel_count == 0:
        return True
    if len(set(lowered)) <= 2 and len(lowered) >= 6:
        return True
    return False


def evaluate_document_quality(
    markdown: str,
    *,
    page_structures: list[dict[str, Any]] | None = None,
    raw_outputs: list[dict[str, Any]] | None = None,
    block_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ocr_confidence = ocr_confidence_score(raw_outputs)
    structure_quality = structure_quality_score(page_structures, block_stats)
    noise_penalty = text_noise_penalty(markdown)
    text_quality = 1 - noise_penalty

    score_components: list[tuple[str, float, float]] = []
    if raw_outputs:
        score_components.append(('ocr_confidence', ocr_confidence, 0.5))
    if page_structures:
        score_components.append(('structure_quality', structure_quality, 0.3))
    if markdown.strip():
        score_components.append(('text_quality', text_quality, 0.2))

    if score_components:
        weighted_score = sum(value * weight for _, value, weight in score_components)
        total_weight = sum(weight for _, _, weight in score_components)
        final_score = weighted_score / total_weight if total_weight else 0.0
    else:
        final_score = 0.0

    final_score = _clamp(final_score)
    if final_score >= _QUALITY_THRESHOLD_A:
        grade = 'A'
        recommendation = 'allow'
    elif final_score >= _QUALITY_THRESHOLD_B:
        grade = 'B'
        recommendation = 'warn'
    else:
        grade = 'C'
        recommendation = 'block'

    return {
        'grade': grade,
        'score': round(final_score, 4),
        'recommendation': recommendation,
        'signals': {
            'ocr_confidence': round(ocr_confidence, 4),
            'structure_quality': round(structure_quality, 4),
            'noise_penalty': round(noise_penalty, 4),
            'text_quality': round(text_quality, 4),
        },
        'thresholds': {
            'A': _QUALITY_THRESHOLD_A,
            'B': _QUALITY_THRESHOLD_B,
        },
    }