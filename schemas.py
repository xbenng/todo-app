"""Pydantic request validation models for all API endpoints.

Usage in route handlers:
    from schemas import CreateTodoRequest, validate_request

    @bp.route("/api/todos", methods=["POST"])
    @validate_request(CreateTodoRequest)
    def add_todo(data: CreateTodoRequest):
        ...
"""

from __future__ import annotations

from functools import wraps
from typing import Literal, Optional

from flask import jsonify, request
from pydantic import BaseModel, Field, ValidationError, field_validator


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def validate_request(model_class):
    """Decorator that validates request.json against a Pydantic model.

    Injects the validated model as the first argument to the handler.
    Returns 400 with error details on validation failure.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                raw = request.json or {}
                validated = model_class.model_validate(raw)
            except ValidationError as e:
                errors = []
                for err in e.errors():
                    field = ".".join(str(loc) for loc in err["loc"])
                    errors.append(f"{field}: {err['msg']}")
                return jsonify({"error": "; ".join(errors)}), 400
            return f(validated, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=6)
    name: str = ""

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        v = v.strip().lower()
        if not v:
            raise ValueError("Email is required")
        return v

    @field_validator("name")
    @classmethod
    def clean_name(cls, v):
        return v.strip()


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        v = v.strip().lower()
        if not v:
            raise ValueError("Email is required")
        return v


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

class CreateTodoRequest(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    priority: Literal["high", "medium", "low", "none"] = "medium"
    section: str = ""

    @field_validator("title", "description", "section")
    @classmethod
    def strip_strings(cls, v):
        return v.strip()


class UpdateTodoRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["open", "completed"]] = None
    priority: Optional[Literal["high", "medium", "low", "none"]] = None
    section: Optional[str] = None
    mark_unread: Optional[bool] = None

    @field_validator("title", "description", "section", mode="before")
    @classmethod
    def strip_strings(cls, v):
        return v.strip() if isinstance(v, str) else v


class ReorderRequest(BaseModel):
    id: str
    direction: Literal["up", "down"]


class MoveToTopRequest(BaseModel):
    id: str


class SortPriorityRequest(BaseModel):
    section: str = ""


class DropRequest(BaseModel):
    id: str
    before_id: Optional[str] = None
    section: Optional[str] = None


class RenameSectionRequest(BaseModel):
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)

    @field_validator("old_name", "new_name")
    @classmethod
    def strip_strings(cls, v):
        return v.strip()


class ReorderSectionRequest(BaseModel):
    section: str = Field(min_length=1)
    before_section: Optional[str] = None

    @field_validator("section", "before_section", mode="before")
    @classmethod
    def strip_strings(cls, v):
        return v.strip() if isinstance(v, str) else v


class UpdateSectionRequest(BaseModel):
    name: str = Field(min_length=1)
    directives: Optional[str] = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v):
        return v.strip()


class ExecuteToolRequest(BaseModel):
    tool: str
    input: dict = {}
    as_agent: bool = False


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class SendChatRequest(BaseModel):
    message: str = Field(min_length=1)
    resume_conv: Optional[int] = None

    @field_validator("message")
    @classmethod
    def strip_message(cls, v):
        return v.strip()


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

class ApproveToolRequest(BaseModel):
    approval_id: str
    approved: bool = False
    always_allow: bool = False


class SetMcpServerRequest(BaseModel):
    server: str
    enabled: bool = False


class SetMcpToolRequest(BaseModel):
    server: str
    tool: str
    disabled: Optional[bool] = None
    auto_approved: Optional[bool] = None


# ---------------------------------------------------------------------------
# EA
# ---------------------------------------------------------------------------

class EaUpdateItemRequest(BaseModel):
    id: str = Field(min_length=1)
    force: bool = False
    message: Optional[str] = None

    @field_validator("id")
    @classmethod
    def strip_id(cls, v):
        return v.strip()


class ResumeConvRequest(BaseModel):
    conversation_id: str = Field(min_length=1)

    @field_validator("conversation_id")
    @classmethod
    def strip_id(cls, v):
        return v.strip()
