"""Dedicated durable subprocess for one Data Root Migration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import uuid4

from cutmaster.application import CutMasterApplication
from cutmaster.infrastructure.storage.local.data_root_migration import (
    LocalDataRootMigrator,
)


def execute_data_root_migration(
    application: CutMasterApplication,
    migration_id: str,
    *,
    worker_id: str | None = None,
    process_id: int | None = None,
    reserved: bool = False,
) -> bool:
    migrations = application.settings.migrations
    resolved_process_id = os.getpid() if process_id is None else process_id
    resolved_worker_id = worker_id or (
        f"data-root-migration-{resolved_process_id}-{uuid4()}"
    )
    adopted = reserved and migrations.store.adopt_reserved_worker(
        migration_id,
        worker_id=resolved_worker_id,
        process_id=resolved_process_id,
    )
    if not adopted and (
        reserved
        or not migrations.store.claim_worker(
            migration_id,
            worker_id=resolved_worker_id,
            process_id=resolved_process_id,
        )
    ):
        return False
    LocalDataRootMigrator(
        application.settings.effective_configuration,
        application.data_root_coordinator,
        migrations.store,
    ).execute(migration_id)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one Data Root Migration")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--migration-id", required=True)
    parser.add_argument("--reserved-worker-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = CutMasterApplication.open(args.config)
    execute_data_root_migration(
        application,
        args.migration_id,
        worker_id=args.reserved_worker_id,
        reserved=args.reserved_worker_id is not None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["execute_data_root_migration", "main"]
