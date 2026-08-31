"""Gemini Keys API — manual Key 1,2,3... management (no auto rotation)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.core.gemini_client import rotator

router = APIRouter(prefix="/api/gemini-keys", tags=["gemini-keys"])


class AddKeyRequest(BaseModel):
    key: str


class UpdateKeyRequest(BaseModel):
    key: str


class SetActiveRequest(BaseModel):
    index: int


@router.get("")
async def list_keys():
    """List all Gemini keys as Key 1, Key 2... with masked values and active marker."""
    return {
        "keys": rotator.list_keys(),
        "active_index": rotator.active_index,
        "count": rotator.key_count,
        "active_masked": rotator.active_key_masked,
    }


@router.post("")
async def add_key(req: AddKeyRequest):
    try:
        result = rotator.add_key(req.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"added": result, "keys": rotator.list_keys(), "active_index": rotator.active_index}


@router.put("/{index}")
async def update_key(index: int, req: UpdateKeyRequest):
    try:
        result = rotator.update_key(index, req.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"updated": result, "keys": rotator.list_keys()}


@router.delete("/{index}")
async def delete_key(index: int):
    try:
        result = rotator.remove_key(index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"deleted": result, "keys": rotator.list_keys(), "active_index": rotator.active_index}


@router.post("/active")
async def set_active(req: SetActiveRequest):
    try:
        result = rotator.set_active(req.index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"active": result, "keys": rotator.list_keys(), "active_index": rotator.active_index}
