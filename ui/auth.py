# ui/auth.py
"""
Authentication & Authorization module for ASDT UI.
Handles user registration, login, JWT tokens, and RBAC.

Phase 6 changes:
  - P3: Default parcel is now auto-generated UUID (parcel_{hex8})
         instead of hardcoded "soil_parcel_001".
  - P3: Test seed users also get unique auto-generated parcels.
"""
import os
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from pymongo import MongoClient
from shared.config import MONGO_URI, MONGO_DB, JWT_SECRET

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

_client = MongoClient(MONGO_URI)
_db = _client[MONGO_DB]


# ── Password hashing ────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash password with salt using SHA-256. Returns (hash, salt)."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return hashed, salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    hashed, _ = _hash_password(password, salt)
    return hashed == stored_hash


# ── JWT Token Management ────────────────────────────────────────────────────

def create_token(user_id: str, role: str, email: str) -> str:
    """Create a JWT token for authenticated user."""
    payload = {
        "user_id": user_id,
        "role": role,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token. Returns payload or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ── User Management ─────────────────────────────────────────────────────────

def register_user(email: str, password: str, role: str, name: str,
                  parcels: list[str] = None) -> dict:
    """
    Register a new user.
    role: 'scientist' or 'farmer'
    parcels: list of parcel IDs (for farmers)

    P3: Default parcel is now auto-generated UUID instead of "soil_parcel_001".
    """
    if role not in ("scientist", "farmer"):
        return {"error": "Role must be 'scientist' or 'farmer'"}

    existing = _db.users.find_one({"email": email.lower()})
    if existing:
        return {"error": "Email already registered"}

    hashed, salt = _hash_password(password)
    user_id = str(uuid.uuid4())

    # P3: Auto-generate a unique parcel ID if none provided
    if not parcels:
        parcels = [f"parcel_{uuid.uuid4().hex[:8]}"]

    user_doc = {
        "user_id": user_id,
        "email": email.lower(),
        "name": name,
        "role": role,
        "password_hash": hashed,
        "password_salt": salt,
        "parcels": parcels,
        "created_at": datetime.now(timezone.utc),
        "last_login": None,
        "preferences": {},
        "active": True,
    }

    _db.users.insert_one(user_doc)

    return {
        "user_id": user_id,
        "email": email.lower(),
        "name": name,
        "role": role,
    }


def login_user(email: str, password: str) -> dict:
    """Authenticate user and return JWT token."""
    user = _db.users.find_one({"email": email.lower(), "active": True})
    if not user:
        return {"error": "Invalid email or password"}

    if not _verify_password(password, user["password_hash"], user["password_salt"]):
        return {"error": "Invalid email or password"}

    _db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"last_login": datetime.now(timezone.utc)}}
    )

    token = create_token(user["user_id"], user["role"], user["email"])

    return {
        "token": token,
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "parcels": user.get("parcels", []),
    }


def get_user(user_id: str) -> Optional[dict]:
    """Get user profile by ID."""
    user = _db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0, "password_salt": 0})
    return user


def get_user_by_email(email: str) -> Optional[dict]:
    """Get user profile by email."""
    user = _db.users.find_one(
        {"email": email.lower()},
        {"_id": 0, "password_hash": 0, "password_salt": 0}
    )
    return user


# ── Profile updates ─────────────────────────────────────────────────────────

def update_profile(user_id: str, updates: dict) -> dict:
    """
    Update user profile fields. Allowed: name, email, parcels, preferences.
    Password changes go through change_password().
    """
    allowed_fields = {"name", "email", "parcels", "preferences", "phone", "location", "farm_name"}
    sanitized = {}

    for key, value in updates.items():
        if key not in allowed_fields:
            continue
        if key == "email":
            value = value.lower().strip()
            # Check email uniqueness
            existing = _db.users.find_one({"email": value, "user_id": {"$ne": user_id}})
            if existing:
                return {"error": "Email already in use by another account"}
        sanitized[key] = value

    if not sanitized:
        return {"error": "No valid fields to update"}

    sanitized["updated_at"] = datetime.now(timezone.utc)

    result = _db.users.update_one(
        {"user_id": user_id},
        {"$set": sanitized},
    )

    if result.matched_count == 0:
        return {"error": "User not found"}

    return get_user(user_id)


def change_password(user_id: str, old_password: str, new_password: str) -> dict:
    """Change a user's password after verifying their current one."""
    user = _db.users.find_one({"user_id": user_id})
    if not user:
        return {"error": "User not found"}

    if not _verify_password(old_password, user["password_hash"], user["password_salt"]):
        return {"error": "Current password is incorrect"}

    if len(new_password) < 6:
        return {"error": "New password must be at least 6 characters"}

    new_hash, new_salt = _hash_password(new_password)
    _db.users.update_one(
        {"user_id": user_id},
        {"$set": {
            "password_hash": new_hash,
            "password_salt": new_salt,
            "password_changed_at": datetime.now(timezone.utc),
        }},
    )

    return {"success": True, "message": "Password changed"}


def add_parcel(user_id: str, parcel_id: str, parcel_name: str = None) -> dict:
    """Add a new parcel association to a user."""
    user = _db.users.find_one({"user_id": user_id})
    if not user:
        return {"error": "User not found"}

    parcels = user.get("parcels", [])
    if parcel_id in parcels:
        return {"error": "Parcel already associated with user"}

    parcels.append(parcel_id)

    # Store parcel metadata
    _db.user_parcels.update_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"$set": {
            "user_id": user_id,
            "parcel_id": parcel_id,
            "parcel_name": parcel_name or parcel_id,
            "added_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"parcels": parcels}},
    )

    return {"success": True, "parcels": parcels}


# ── Audit Logging ────────────────────────────────────────────────────────────

def log_audit(user_id: str, action: str, details: dict):
    """Log a UI action to the audit trail."""
    _db.audit_log.insert_one({
        "user_id": user_id,
        "action": action,
        "details": details,
        "timestamp": datetime.now(timezone.utc),
    })


def seed_test_users():
    """Kept for API compatibility — all accounts are created through self-registration."""
    return []