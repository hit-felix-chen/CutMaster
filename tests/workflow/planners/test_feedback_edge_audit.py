import json
from types import SimpleNamespace

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.planners.arrangement_architect import ArrangementArchitectAgent
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.planners import _checkpoint_feedback
from cutmaster.workflow.planners.story_editor import StoryEditorAgent


def test_candidate_reason_survives_record_failure_into_targeted_prompt() -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "group_id": f"group_{index:03d}",
            "source_segment_id": f"segment_{segment:04d}",
            "narrative_role": "development",
            "content_description": f"local event {index}",
            "target_emotion": "focused",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "desired_duration_sec": 2.0,
            "planned_duration_ms": 2000,
            "planned_duration_sec": 2.0,
            "output_start_sec": float((index - 1) * 2),
            "output_end_sec": float(index * 2),
            "continuity_from_previous": "continues",
            "required_visible_subjects": ["local goalkeeper"],
        }
        for index, segment in enumerate([1, 3, 5], 1)
    ]
    artifacts = {
        "video_description": {
            "segments": [
                {
                    "segment_id": f"segment_{index:04d}",
                    "time_range": {
                        "start_sec": float(index * 10),
                        "end_sec": float(index * 10 + 10),
                    },
                }
                for index in range(1, 6)
            ]
        }
    }
    packages = []

    class Context:
        def get_artifact(self, name, default=None):
            return artifacts.get(name, default)

        def set_artifact(self, name, value):
            artifacts[name] = value

        def call_prompt(self, *, package, validate_business, **_kwargs):
            packages.append(package)
            return validate_business(
                {"slots": [{**slots[1], "source_segment_id": "segment_0004"}]}
            )

    config = SimpleNamespace(
        llm=LLMConfig(model="test", base_url="", api_key="test"),
        planners=SimpleNamespace(
            arrangement_architect=SimpleNamespace(max_model_requests=3),
        ),
    )
    context = Context()
    team = object.__new__(ASTERTeam)
    team.context = context
    team.story_editor = StoryEditorAgent(config, context)
    team.arrangement_architect = ArrangementArchitectAgent(config, context)
    evidence = {
        "group_id": "group_002",
        "slot_id": "slot_02",
        "reason_code": "required_subject_not_visually_confirmed",
        "diagnosis": "Only spectators are visible; the local goalkeeper is absent.",
        "repair_requirement": "Choose a supported goalkeeper event.",
        "visible_description": "Only spectators are visible.",
        "visible_subjects": ["spectators"],
        "timestamp": "00:00:30,000-00:00:32,000",
    }
    diagnostics = {
        "reason_code": "insufficient_visually_grounded_candidates",
        "diagnosis": "No complete trajectory survived.",
        "repair_requirement": "Reassign the failed group.",
        "failed_group_ids": ["group_002"],
        "failed_parent_group_ids": ["group_002"],
        "semantic_zero_candidate_group_ids": ["group_002"],
        "candidate_rejections": [
            {
                "group_id": "group_002",
                "candidate_rejections": [dict(evidence)],
            }
            for _ in range(3)
        ] + [
            {
                "group_id": "group_001",
                "slot_id": "slot_01",
                "reason_code": "visually_static",
                "diagnosis": "unrelated healthy group rejection",
            }
        ],
    }
    team.record_failure(
        attempt=1,
        error="No candidate",
        diagnostics=diagnostics,
        failed_slots=[{**slots[1], "parent_group_id": "group_002"}],
    )
    feedback = json.loads(json.dumps(_checkpoint_feedback(artifacts["planners_feedback"])))
    artifacts["planners_feedback"] = feedback
    team.repair_groups(slots, feedback["diagnostics"])

    assert "Only spectators are visible" in packages[0].user_prompt
    assert "required_subject_not_visually_confirmed" in packages[0].user_prompt
    assert "Choose a supported goalkeeper event" in packages[0].user_prompt
    assert packages[0].user_prompt.count(evidence["timestamp"]) == 1
    assert "unrelated healthy group rejection" not in packages[0].user_prompt

    # A subsequent backend failure must not erase this evidence before a full
    # Arrangement consumes the accumulated feedback (including after resume).
    team.record_failure(
        attempt=1,
        error="local window infeasible",
        diagnostics={
            "reason_code": "arrangement_capacity_or_duration",
            "failed_parent_group_ids": ["group_002"],
        },
        failed_slots=[slots[1]],
    )
    persisted = artifacts["planners_feedback"]["candidate_failure_evidence"]
    assert len(persisted) == 1
    assert persisted[0]["occurrences"] == 3
    assert persisted[0]["visible_subjects"] == ["spectators"]
