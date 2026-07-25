from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from cutmaster.llm import generate_text, request_json_with_retries
from cutmaster.models import LLMConfig
from cutmaster.timecode import format_time, parse_time

if TYPE_CHECKING:
    from cutmaster.workflow_context import WorkflowContext

SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)"
)
SPEAKER_RE = re.compile(r"^(Speaker\s+\d+):\s*(.*)$", re.IGNORECASE | re.DOTALL)
TERMINAL_RE = re.compile(r"[。！？.!?][\"'”’）)]*$")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
SYSTEM_PROMPT = (
    "You reconstruct complete spoken sentences from adjacent ASR subtitle fragments. "
    "Return strict JSON only. Never rewrite dialogue and only return the provided "
    "candidate labels. Cue IDs are anchors, not candidate labels."
)


def generate_boundary_decisions(prompt: str, config: LLMConfig, system_prompt: str) -> str:
    return generate_text(prompt, config, system_prompt)


@dataclass(frozen=True)
class Cue:
    cue_id: int
    start: float
    end: float
    speaker: str | None
    text: str

    def anchor(self) -> dict[str, object]:
        return {
            "cue_id": self.cue_id,
            "start": format_time(self.start),
            "end": format_time(self.end),
            "text": self.text,
        }


def parse_srt(content: str) -> list[Cue]:
    cues: list[Cue] = []
    for cue_id, start, end, raw_text in SRT_BLOCK_RE.findall(content.strip()):
        flattened = " ".join(part.strip() for part in raw_text.splitlines() if part.strip())
        speaker_match = SPEAKER_RE.match(flattened)
        speaker = speaker_match.group(1) if speaker_match else None
        text = speaker_match.group(2).strip() if speaker_match else flattened.strip()
        if text:
            cues.append(Cue(int(cue_id), parse_time(start), parse_time(end), speaker, text))
    if not cues:
        raise ValueError("SRT contains no usable dialogue cues")
    return cues


def candidate_passages(cues: list[Cue], max_gap_sec: float = 1.5) -> list[list[Cue]]:
    passages: list[list[Cue]] = []
    current: list[Cue] = []
    for cue in cues:
        if current:
            previous = current[-1]
            boundary = (
                cue.speaker != previous.speaker
                or cue.start - previous.end > max_gap_sec
                or bool(TERMINAL_RE.search(previous.text.strip()))
            )
            if boundary:
                if len(current) > 1:
                    passages.append(current)
                current = []
        current.append(cue)
    if len(current) > 1:
        passages.append(current)
    return passages


def _chunk_passages(passages: list[list[Cue]], max_chars: int = 18_000) -> list[list[list[Cue]]]:
    chunks: list[list[list[Cue]]] = []
    current: list[list[Cue]] = []
    current_chars = 0
    for passage in passages:
        size = sum(len(cue.text) + 60 for cue in passage)
        if current and current_chars + size > max_chars:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(passage)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def _build_prompt(passages: list[list[Cue]]) -> str:
    candidates = [
        {
            "candidate_label": f"candidate_{index:03d}",
            "speaker": passage[0].speaker,
            "cues": [cue.anchor() for cue in passage],
        }
        for index, passage in enumerate(passages, start=1)
    ]
    return f"""# ASR dialogue-fragment reconstruction

The input contains candidate passages made only from adjacent cues by the same speaker.
Identify cue sequences that together form one grammatically and semantically complete spoken sentence.

Rules:
1. Return only candidate labels whose complete cue sequence should actually be merged.
2. Omit candidates whose cues should remain separate.
3. Do not merge independent complete sentences, even when the speaker is unchanged.
4. Continuations split by ASR length limits, commas, clauses, numbers, or delayed sentence-final punctuation should be merged.
5. Do not rewrite text or timestamps.
6. Values such as cue_id are subtitle anchors. Never return a cue_id.

Candidates:
{json.dumps(candidates, ensure_ascii=False)}

Return the candidate labels whose complete cue sequence should be merged:
{{"merge_candidate_ids": ["candidate_001", "candidate_002"]}}
"""


def _validate_decisions(raw_ids: object, passages: list[list[Cue]]) -> list[list[int]]:
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list):
        raise ValueError("merge_candidate_ids must be an array")
    groups: list[list[int]] = []
    seen: set[int] = set()
    for raw_id in raw_ids:
        try:
            if isinstance(raw_id, str) and raw_id.startswith("candidate_"):
                candidate_id = int(raw_id.removeprefix("candidate_"))
            else:
                candidate_id = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid dialogue candidate label: {!r}", raw_id)
            continue
        if candidate_id < 1 or candidate_id > len(passages):
            logger.warning(
                "Ignoring out-of-range dialogue candidate ID {} (batch has {} candidates)",
                candidate_id,
                len(passages),
            )
            continue
        if candidate_id in seen:
            logger.warning("Ignoring duplicate dialogue candidate ID: {}", candidate_id)
            continue
        seen.add(candidate_id)
        groups.append([cue.cue_id for cue in passages[candidate_id - 1]])
    return groups


def _join_text(cues: list[Cue]) -> str:
    result = cues[0].text.strip()
    for cue in cues[1:]:
        text = cue.text.strip()
        separator = " " if not CJK_RE.search(result + text) and result[-1:].isalnum() and text[:1].isalnum() else ""
        result += separator + text
    return result


def build_dialogue_document(
    cues: list[Cue],
    merge_groups: list[list[int]],
    source_srt: Path,
    model: str,
) -> dict[str, object]:
    by_start = {group[0]: set(group) for group in merge_groups}
    merged_ids = {cue_id for group in merge_groups for cue_id in group}
    sentences: list[dict[str, object]] = []
    index = 0
    while index < len(cues):
        cue = cues[index]
        group_ids = by_start.get(cue.cue_id)
        if group_ids:
            anchors = []
            while index < len(cues) and cues[index].cue_id in group_ids:
                anchors.append(cues[index])
                index += 1
        else:
            if cue.cue_id in merged_ids:
                raise ValueError(f"Merge group does not begin at cue {cue.cue_id}")
            anchors = [cue]
            index += 1
        sentence_id = len(sentences) + 1
        sentences.append(
            {
                "sentence_id": sentence_id,
                "speaker": anchors[0].speaker,
                "start": format_time(anchors[0].start),
                "end": format_time(anchors[-1].end),
                "text": _join_text(anchors),
                "was_merged": len(anchors) > 1,
                "anchors": [anchor.anchor() for anchor in anchors],
            }
        )
    operations = [
        {
            "sentence_id": sentence["sentence_id"],
            "cue_ids": [anchor["cue_id"] for anchor in sentence["anchors"]],
            "start": sentence["start"],
            "end": sentence["end"],
        }
        for sentence in sentences
        if sentence["was_merged"]
    ]
    return {
        "schema_version": "1.0",
        "source_srt": str(source_srt),
        "postprocessor": {"type": "llm_boundary_selection", "model": model},
        "statistics": {
            "source_cue_count": len(cues),
            "sentence_count": len(sentences),
            "merged_sentence_count": len(operations),
        },
        "sentences": sentences,
        "merge_operations": operations,
    }


def write_merged_srt(path: Path, document: dict[str, object]) -> None:
    blocks = []
    for sentence in document["sentences"]:
        speaker = f"{sentence['speaker']}: " if sentence["speaker"] else ""
        blocks.append(
            f"{sentence['sentence_id']}\n{sentence['start']} --> {sentence['end']}\n"
            f"{speaker}{sentence['text']}\n"
        )
    path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")


def postprocess_dialogues(
    source_srt: Path,
    output_dir: Path,
    config: LLMConfig,
    generator: Callable[[str, LLMConfig, str], str] = generate_boundary_decisions,
    context: WorkflowContext | None = None,
) -> tuple[Path, Path]:
    cues = parse_srt(source_srt.read_text(encoding="utf-8-sig"))
    passages = candidate_passages(cues)
    chunks = _chunk_passages(passages)
    logger.info(
        "Dialogue postprocessing: {} cues, {} candidate passages, {} model batches",
        len(cues),
        len(passages),
        len(chunks),
    )
    decisions: list[list[list[int]] | None] = [None] * len(chunks)
    worker_count = max(1, min(config.max_concurrency, len(chunks)))

    def process_chunk(index: int, chunk: list[list[Cue]]) -> tuple[int, list[list[int]]]:
        logger.info("Dialogue model batch {}/{} started", index + 1, len(chunks))
        prompt = _build_prompt(chunk)
        operation = f"Dialogue reconstruction batch {index + 1}/{len(chunks)}"
        if context is None:
            groups = request_json_with_retries(
                lambda: generator(prompt, config, SYSTEM_PROMPT),
                config,
                operation=operation,
                validate=lambda parsed: _validate_decisions(
                    parsed.get("merge_candidate_ids"), chunk
                ),
            )
        else:
            groups = context.call_json(
                operation=operation,
                prompt=prompt,
                config=config,
                system_prompt=SYSTEM_PROMPT,
                validate=lambda parsed: _validate_decisions(
                    parsed.get("merge_candidate_ids"), chunk
                ),
            )
        return index, groups

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="dialogue-llm") as executor:
        futures = {
            executor.submit(process_chunk, index, chunk): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            index, groups = future.result()
            decisions[index] = groups
            logger.info("Dialogue model batch {}/{} completed", index + 1, len(chunks))

    merge_groups = [group for batch in decisions if batch is not None for group in batch]
    document = build_dialogue_document(cues, merge_groups, source_srt, config.model)
    dialogue_path = output_dir / "dialogues.json"
    merged_srt_path = output_dir / "dialogue_merged.srt"
    dialogue_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_merged_srt(merged_srt_path, document)
    logger.info(
        "Dialogue postprocessing complete: {} sentences, {} merged sentences",
        document["statistics"]["sentence_count"],
        document["statistics"]["merged_sentence_count"],
    )
    return merged_srt_path, dialogue_path
