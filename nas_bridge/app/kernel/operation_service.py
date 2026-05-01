"""Kernel-level entry point for the Operation lifecycle service.

This module is the canonical kernel-vocabulary import path for code
that lives inside ``kernel/`` or wants to depend on the generic
Operation primitive without crossing back into product layering. The
implementation body still lives in
``app.services.remote_task_service.RemoteTaskService`` -- a follow-up
PR will move the body here once call sites have migrated.

Usage:
    from app.kernel.operation_service import KernelOperationService

The alias preserves the existing wire shape (request / response
schemas, method names, lease semantics) so behaviors that rely on
RemoteTaskService keep working without code change.
"""

from __future__ import annotations

from ..services.remote_task_service import RemoteTaskService as KernelOperationService

__all__ = ["KernelOperationService"]
