"""Prepare first-retained-candidate ablations from archived benchmark artifacts.

This command performs no model calls and no rendering. Its raw scripts must go
through the same plan compiler/source-window optimizer and renderer as control.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cutmaster.workflow.planners.edit_composer import (
    flatten_trajectory_path,
    path_to_script,
    select_first_trajectory_path,
    validate_selected_trajectory_path,
)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact(root: Path, record: dict, key: str) -> Path:
    declared = record.get("artifacts", {}).get(key)
    if declared:
        path = root / declared
        if path.is_file():
            return path
    directory = (
        root / "runs" / record["run_id"] / "task_outputs" / record["task_id"]
        / "artifacts" / "cutmaster" / "managed_artifacts" / "planners" / key
    )
    matches = list(directory.glob("*.json"))
    if len(matches) != 1:
        raise ValueError(f"Expected one {key} artifact for {record['task_id']}: {directory}")
    return matches[0]


def replay(root: Path, record: dict) -> tuple[list[dict], dict]:
    slots = _read(_artifact(root, record, "edit_plan"))
    pool = _read(_artifact(root, record, "candidate_pool"))
    segments = _read(_artifact(root, record, "planning_segments"))
    path, selection, _ = select_first_trajectory_path(slots, pool, segments)
    validate_selected_trajectory_path(
        slots, pool, path, selection["selected_trajectory_ids"], segments
    )
    source_script = _read(_artifact(root, record, "script_raw"))
    if not source_script:
        raise ValueError("Source script is empty")
    script = path_to_script(
        slots, flatten_trajectory_path(path), Path(source_script[0]["video_name"])
    )
    if [s['slot_id'] for s in script] != [s['slot_id'] for s in source_script]:
        raise ValueError("Slot sequence changed from source script")
    for item, old in zip(script, source_script, strict=True):
        if old.get('dialogue_anchor') is not None:
            if (
                item.get('dialogue_anchor') != old['dialogue_anchor']
                or item['timestamp'] != old['timestamp']
            ):
                raise ValueError(f"Anchor changed for {item['slot_id']}")
        for key in ['output_start_sec', 'output_end_sec', 'planned_duration_ms']:
            if item[key] != old[key]:
                raise ValueError(f"Output timing changed for {item['slot_id']}: {key}")
    selection.update({
        "source_run_id": record["run_id"],
        "source_task_id": record["task_id"],
        "additional_model_requests": 0,
        "historical_retrieval_and_validation_reused": True,
        "changed_slots": sum(
            a['candidate_id'] != b['candidate_id']
            for a, b in zip(script, source_script, strict=True)
        ),
    })
    return script, selection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-root', type=Path, required=True)
    parser.add_argument('--source-run', default='cutmaster_overlap')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--task-id', action='append')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.dry_run and args.output is None:
        parser.error('--output is required unless --dry-run is supplied')
    # Refuse overwriting any earlier experiment or source artifact tree.
    if not args.dry_run and args.output.exists():
        parser.error('--output must be a new directory')
    manifest = args.benchmark_root / 'runs' / args.source_run / 'run_outputs.jsonl'
    records = [
        json.loads(line) for line in manifest.read_text().splitlines() if line.strip()
    ]
    if args.task_id:
        missing = set(args.task_id) - {r['task_id'] for r in records}
        if missing:
            parser.error(f'Unknown tasks: {sorted(missing)}')
        records = [r for r in records if r['task_id'] in args.task_id]
    failures = []
    for record in records:
        try:
            if record['status'] != 'success':
                raise ValueError('Source task is not successful')
            script, selection = replay(args.benchmark_root, record)
            if not args.dry_run:
                output = args.output / record['task_id']
                output.mkdir(parents=True)
                for name, data in [('raw_script.json', script), ('selection.json', selection)]:
                    (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            print(f"{record['task_id']}: ready, {selection['changed_slots']} changed slots, no model calls")
        except (ValueError, KeyError, OSError) as error:
            failures.append({'task_id': record['task_id'], 'error': str(error)})
            print(f"{record['task_id']}: FAILED: {error}")
    if not args.dry_run:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'preparation_summary.json').write_text(json.dumps({
            'status': 'prepared_not_rendered', 'source_run': args.source_run,
            'tasks': len(records), 'failures': failures, 'additional_model_requests': 0,
        }, ensure_ascii=False, indent=2) + '\n')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
