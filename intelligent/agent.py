# intelligent/agent.py
"""
LangChain Diagnostic Agent — Intelligent Layer.

Uses Ollama Cloud (qwen3.5) via the native Ollama client pointed at
https://ollama.com, authenticated with an API key.

PRODUCTION RULES (CRITICAL):
  1. The agent NEVER makes up data. If a tool returns "no data", the agent
     reports "no data" — not a guess, not an assumption, not a fabrication.
  2. The agent NEVER invents user names. It uses the user_context provided.
  3. The agent uses tools to verify facts before stating them.
  4. If Ollama Cloud is unreachable, the agent returns a clear connection
     error — it does NOT fall back to making things up.

Phase 6 changes:
  - P0: System prompts now inject the user's parcel soil type (from MongoDB)
         instead of the global ACTIVE_SOIL_TYPE. The diagnosis tool passes
         the user's soil type to SoilIntelligenceAgent.
  - The interactive REPL still uses ACTIVE_SOIL_TYPE (no user context).

Run standalone:  python -m intelligent.agent
"""
import json
import os
import sys
import socket

from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import ToolMessage

from service_layer.ditto_client import get_thing
from intelligent.neo4j_kg import (
    diagnose_from_sensor_causes, get_soil_components,
    get_all_depletion_states, get_management_for_state,
)
from intelligent.soil_intelligence_agent import SoilIntelligenceAgent
from shared.influx_io import get_latest
from shared.mongo_io import get_recent_events, get_asset_metadata, get_recent_cross_domain
from shared.config import (
    OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY,
    SENSOR_FIELDS, ACTIVE_SOIL_TYPE, DEPLETION_STATES,
    get_parcel_soil_type,
)

# Per-user data sources (lazy import to avoid circular dependency)
try:
    from ui.data_sources import get_user_latest_readings, get_data_availability
except ImportError:
    get_user_latest_readings = None
    get_data_availability = None


# ── Connectivity check ────────────────────────────────────────────────────────

def check_ollama_reachable(timeout: int = 5) -> tuple[bool, str]:
    """
    Verify that Ollama Cloud is reachable. Returns (success, message).
    Used to provide clear errors instead of confusing 'Errno -2' messages.
    """
    if not OLLAMA_BASE_URL:
        return False, "OLLAMA_BASE_URL is not configured"
    if not OLLAMA_API_KEY:
        return False, "OLLAMA_API_KEY is not configured"

    # Extract hostname for DNS check
    from urllib.parse import urlparse
    parsed = urlparse(OLLAMA_BASE_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(host)
    except socket.gaierror:
        return False, f"Cannot resolve {host}. Check your internet connection or DNS."
    except socket.timeout:
        return False, f"DNS lookup for {host} timed out."

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except (OSError, socket.timeout) as e:
        return False, f"Cannot reach {host}:{port}. {type(e).__name__}: {e}"

    return True, "OK"


# ── Agent Tools ───────────────────────────────────────────────────────────────

# Per-call user context (set before _run_agent_loop, read by user-aware tools)
_current_user_context = {}


def _set_user_context(user_id: str = None, parcel_id: str = None,
                       user_name: str = None, role: str = None):
    """Set user context for the current agent call. Tools read from this."""
    global _current_user_context
    _current_user_context = {
        "user_id": user_id,
        "parcel_id": parcel_id,
        "user_name": user_name,
        "role": role,
    }


@tool
def get_user_context(query: str = "") -> str:
    """
    Get the current user's profile context (name, role, parcel ID).
    Use this whenever you need to address the user by name or know their role.
    Returns the user's actual identity — NEVER make up names yourself.
    """
    ctx = _current_user_context
    if not ctx or not ctx.get("user_id"):
        return json.dumps({
            "available": False,
            "message": "No user context. Address the user generically (e.g., 'the user', 'you')."
        })

    # P0: Include the user's soil type
    soil_type = ACTIVE_SOIL_TYPE
    if ctx.get("user_id") and ctx.get("parcel_id"):
        soil_type = get_parcel_soil_type(ctx["user_id"], ctx["parcel_id"])

    return json.dumps({
        "available": True,
        "user_name": ctx.get("user_name", "Unknown"),
        "role": ctx.get("role", "user"),
        "parcel_id": ctx.get("parcel_id"),
        "soil_type": soil_type,
    })


@tool
def get_user_soil_data(query: str = "") -> str:
    """
    Get the user's actual soil sensor readings for THEIR parcel.
    Returns ONLY what is in the database. If a field has no data,
    that field is OMITTED from the response — it does NOT exist.

    This is the PRIMARY tool for any question about soil values, health,
    or recommendations. ALWAYS call this before answering any soil question.
    """
    ctx = _current_user_context
    user_id = ctx.get("user_id")
    parcel_id = ctx.get("parcel_id")

    if not user_id or not parcel_id:
        return json.dumps({
            "error": "no_user_context",
            "message": "Cannot retrieve data — no user is logged in."
        })

    if get_data_availability is None:
        return json.dumps({
            "error": "data_module_unavailable",
            "message": "Data service is not available."
        })

    avail = get_data_availability(user_id, parcel_id)

    if not avail.get("configured"):
        return json.dumps({
            "configured": False,
            "message": "User has not yet configured a data source. They need to either connect a sensor or enter data manually through onboarding.",
            "action_required": "User must complete onboarding before any soil questions can be answered."
        })

    if not avail.get("readings"):
        return json.dumps({
            "configured": True,
            "mode": avail.get("mode"),
            "has_data": False,
            "message": f"User is set up in '{avail.get('mode')}' mode but has not yet provided any soil measurements.",
            "fields_missing": avail.get("fields_missing", []),
        })

    # We have real data — return it
    readings = avail["readings"]

    # P0: Include the user's soil type
    soil_type = get_parcel_soil_type(user_id, parcel_id)

    return json.dumps({
        "configured": True,
        "mode": avail.get("mode"),
        "has_data": True,
        "soil_type": soil_type,
        "readings": {
            field: {
                "value": info["value"],
                "age_minutes": info["age_minutes"],
                "source": info["source"],
            }
            for field, info in readings.items()
        },
        "fields_with_data": avail.get("fields_with_data", []),
        "fields_missing": avail.get("fields_missing", []),
        "completion_pct": avail.get("completion_pct", 0),
        "stale_fields": avail.get("stale_fields", []),
    }, indent=2)


@tool
def get_twin_state(query: str = "") -> str:
    """Returns the global soil parcel twin state. Use ONLY when explicitly needed for system-level info — for user-specific data, prefer get_user_soil_data."""
    try:
        return json.dumps(get_thing(), indent=2)
    except Exception as e:
        return f"Error retrieving twin state: {e}"


@tool
def get_sensor_reading(field: str) -> str:
    """Returns the latest GLOBAL value of a soil sensor field (system-level, not user-specific).
    For user-specific data, use get_user_soil_data instead.
    Valid fields: soil_moisture_pct, soil_temp_c, bulk_density_g_cm3."""
    val = get_latest(field.strip())
    if val is None:
        return f"NO DATA available for '{field}'. Do not invent a value. Tell the user no data exists."
    return f"{field} = {val:.3f}"


@tool
def get_all_sensor_readings(query: str = "") -> str:
    """Returns all current GLOBAL soil sensor readings (system-level).
    For user-specific data, use get_user_soil_data instead."""
    readings = {}
    for f in SENSOR_FIELDS:
        v = get_latest(f)
        readings[f] = round(v, 3) if v is not None else "NO DATA"
    return json.dumps(readings, indent=2)


@tool
def run_soil_depletion_diagnosis(soil_type: str = "") -> str:
    """Runs the full 6-step soil depletion diagnostic pipeline on the user's data.
    Detects depletion states S1-S8 and generates recommendations.
    Only works if user has actual soil data — call get_user_soil_data first to verify."""
    ctx = _current_user_context
    user_id = ctx.get("user_id")
    parcel_id = ctx.get("parcel_id")

    # Verify user has data first
    if user_id and parcel_id and get_data_availability:
        avail = get_data_availability(user_id, parcel_id)
        if not avail.get("configured") or not avail.get("readings"):
            return json.dumps({
                "error": "no_data",
                "message": "Cannot run diagnosis — user has no soil measurements yet. Onboarding required first."
            })

    # P0: Resolve the user's soil type for the diagnosis
    effective_soil_type = soil_type or None
    if not effective_soil_type and user_id and parcel_id:
        effective_soil_type = get_parcel_soil_type(user_id, parcel_id)

    agent = SoilIntelligenceAgent(soil_type=effective_soil_type)
    result = agent.run_full_pipeline()
    return json.dumps({
        "soil_type":        result.get("soil_type"),
        "depletion_states": result.get("depletion_states"),
        "primary_state":    result.get("primary_state"),
        "recommendations":  result.get("recommendations"),
        "should_escalate":  result.get("should_escalate"),
        "explanation":      result.get("explanation"),
    }, indent=2)


@tool
def get_depletion_state_info(state_code: str) -> str:
    """Get details about a specific soil depletion state (S1-S8).
    Returns definition, trigger, recovery action, and KG management actions."""
    info = DEPLETION_STATES.get(state_code.upper(), {})
    kg_actions = get_management_for_state(state_code.upper())
    return json.dumps({"state": info, "management_actions": kg_actions}, indent=2)


@tool
def get_recent_event_log(query: str = "") -> str:
    """Returns the 10 most recent system events (alerts, state changes, cross-domain)."""
    events = get_recent_events(10)
    return json.dumps(events, indent=2, default=str)


@tool
def get_parcel_metadata(query: str = "") -> str:
    """Returns static parcel metadata (location, soil type, texture, management history)."""
    return json.dumps(get_asset_metadata(), indent=2, default=str)


@tool
def get_soil_component_list(query: str = "") -> str:
    """Returns soil components (physical, chemical, biological) from the knowledge graph."""
    return json.dumps(get_soil_components(), indent=2)


@tool
def get_cross_domain_status(query: str = "") -> str:
    """Returns recent cross-domain messages exchanged with Plant DT and Biotic Pod DT.
    Shows what soil data was published and what plant/biotic data was consumed."""
    messages = get_recent_cross_domain(10)
    return json.dumps({
        "recent_messages": messages,
    }, indent=2, default=str)


TOOLS = [
    get_user_context,
    get_user_soil_data,
    get_twin_state,
    get_sensor_reading,
    get_all_sensor_readings,
    run_soil_depletion_diagnosis,
    get_depletion_state_info,
    get_recent_event_log,
    get_parcel_metadata,
    get_soil_component_list,
    get_cross_domain_status,
]


# ── System prompts per role ───────────────────────────────────────────────────

ANTI_HALLUCINATION_RULES = """
CRITICAL RULES — VIOLATING THESE IS A FAILURE:
1. NEVER invent or guess data. If a tool says "NO DATA" or "no_data", you MUST say so.
2. NEVER make up names. Always call get_user_context to learn the user's actual name.
   If their name is not available, address them as "you" — never "John", "Sarah", or any made-up name.
3. NEVER assume location, crop, or any other detail not provided by tools.
4. NEVER fabricate sensor values. If asked about pH/nitrogen/etc., call get_user_soil_data first.
5. If get_user_soil_data returns has_data: false, tell the user clearly:
   "You haven't provided any soil measurements yet. Please complete onboarding to enter your soil data."
6. If a tool fails or returns an error, say so explicitly. Do not paper over errors with guesses.
7. Always call get_user_context FIRST in any conversation to know who you are talking to.
"""

# NOTE: System prompts are built dynamically in _run_agent_loop() to include
# the user's actual soil type from MongoDB, not the global ACTIVE_SOIL_TYPE.

_SCIENTIST_BASE = (
    "You are an expert Soil Scientist AI assistant for the Agentic Soil Digital Twin (ASDT). "
    "You are speaking with a Soil Scientist who wants full technical detail. "
    "Your focus is strictly on SOIL: monitoring soil parameters (physical, chemical, biological), "
    "detecting soil depletion states (S1-S8), and recommending soil management actions. "
    "You do NOT diagnose plant symptoms, recommend crops, or provide market advice. "
    + ANTI_HALLUCINATION_RULES +
    "Provide technical detail: exact values with units, confidence scores, KG relationships, "
    "threshold comparisons, and cross-domain sync status when relevant. "
    "Cite specific parameter values and depletion state codes (S1-S8). "
    "Available depletion states: S1(Nutrient Depleted), S2(Acidified), S3(Salinized), "
    "S4(Compacted), S5(Water-Stressed), S6(OM Depleted), S7(Biologically Inactive), "
    "S8(Multi-Factor Depleted)."
)

_FARMER_BASE = (
    "You are a friendly Soil Advisory AI for a farmer using the Agentic Soil Digital Twin. "
    "You are speaking with a farmer who needs simple, actionable advice in plain language. "
    "Your focus is strictly on SOIL health and what the farmer should DO about it. "
    "You do NOT diagnose plant diseases, recommend crop varieties, or give market advice. "
    + ANTI_HALLUCINATION_RULES +
    "Keep answers short and practical. Use everyday language, not technical jargon. "
    "When recommending products, give the product name, how much to apply per hectare, "
    "and when to apply it. Explain WHY in one simple sentence. "
    "If the soil has multiple problems, tell the farmer the ORDER to fix them and why. "
    "Do not overwhelm with data tables — summarise what is good and what needs fixing. "
    "If the user has no data yet, gently encourage them to complete the onboarding process "
    "to enter their soil measurements — do NOT pretend to have data you don't have."
)

# Legacy SYSTEM_PROMPTS dict — kept for backward compat with interactive REPL
SYSTEM_PROMPTS = {
    "scientist": _SCIENTIST_BASE + f"\nCurrent soil type: {ACTIVE_SOIL_TYPE}.",
    "farmer": _FARMER_BASE + f"\nCurrent soil type: {ACTIVE_SOIL_TYPE}.",
}


def _build_system_prompt(role: str, soil_type: str = None) -> str:
    """Build a system prompt with the user's actual soil type."""
    st = soil_type or ACTIVE_SOIL_TYPE
    if role == "scientist":
        return _SCIENTIST_BASE + f"\nCurrent soil type: {st}."
    return _FARMER_BASE + f"\nCurrent soil type: {st}."


# ── LLM setup ────────────────────────────────────────────────────────────────

llm = ChatOllama(
    base_url=OLLAMA_BASE_URL,
    model=OLLAMA_MODEL,
    temperature=0,
    client_kwargs={
        "headers": {"Authorization": f"Bearer {OLLAMA_API_KEY}"},
    },
).bind_tools(TOOLS)


# ── Agent loop ────────────────────────────────────────────────────────────────

def _run_agent_loop(question: str, role: str = "scientist",
                    history: list = None, max_iterations: int = 6,
                    user_context: dict = None) -> tuple:
    """
    Tool-calling agent loop with conversation history.
    Returns (answer_text, updated_history).

    user_context: optional dict with {user_id, parcel_id, user_name, role}.
                  This is injected into the system prompt and made available
                  to user-aware tools via get_user_context().
    """
    # Set per-call user context (read by user-aware tools)
    if user_context:
        _set_user_context(**user_context)
    else:
        _set_user_context()

    # Pre-flight: check Ollama is reachable so we give clear errors
    ok, msg = check_ollama_reachable()
    if not ok:
        return (
            f"I cannot reach the AI service right now ({msg}). "
            "Please check your internet connection and try again. "
            "If the problem persists, contact your administrator.",
            history or [],
        )

    # P0: Resolve the user's soil type for the system prompt
    user_soil_type = None
    if user_context and user_context.get("user_id") and user_context.get("parcel_id"):
        user_soil_type = get_parcel_soil_type(
            user_context["user_id"], user_context["parcel_id"]
        )

    base_prompt = _build_system_prompt(role, user_soil_type)

    # Inject user context into the system prompt as a hard fact
    if user_context and user_context.get("user_name"):
        ctx_block = (
            f"\n\nYOU ARE TALKING TO: {user_context['user_name']} "
            f"(role: {user_context.get('role', 'user')}, "
            f"parcel: {user_context.get('parcel_id', 'unknown')}, "
            f"soil type: {user_soil_type or 'unknown'}). "
            "Use this name only — do NOT use any other name. "
            "Call get_user_context if you need to confirm."
        )
        system_prompt = base_prompt + ctx_block
    else:
        system_prompt = base_prompt + (
            "\n\nNo specific user is logged in. Address the user generically as 'you'. "
            "Do NOT make up a name."
        )

    if history is None:
        messages = [("system", system_prompt), ("human", question)]
    else:
        messages = [("system", system_prompt)] + history + [("human", question)]

    tool_map = {t.name: t for t in TOOLS}

    for i in range(max_iterations):
        try:
            response = llm.invoke(messages)
        except Exception as e:
            err = str(e)
            # Provide friendlier error messages for common issues
            if "Errno -2" in err or "Name or service not known" in err:
                return (
                    "I cannot reach the AI service right now (DNS resolution failed for ollama.com). "
                    "Please check your internet connection.",
                    history or [],
                )
            if "401" in err or "unauthorized" in err.lower():
                return (
                    "AI service authentication failed. Please verify the OLLAMA_API_KEY is correct.",
                    history or [],
                )
            return (f"AI service error: {err}", history or [])

        messages.append(response)

        if not response.tool_calls:
            new_history = messages[1:]
            return response.content, new_history

        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            print(f"  [AGENT] Calling tool: {tool_name}({json.dumps(tool_args)[:80]})")

            if tool_name in tool_map:
                try:
                    result = tool_map[tool_name].invoke(tool_args)
                except Exception as e:
                    result = f"Tool error: {e}"
            else:
                result = f"Unknown tool: {tool_name}"

            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    new_history = messages[1:]
    return "I ran out of steps processing your request. Please try a simpler question.", new_history


def ask(question: str, role: str = "scientist",
        user_context: dict = None) -> str:
    """
    Ask the ASDT agent a single question (no history). For programmatic use.

    user_context: {user_id, parcel_id, user_name, role}
    """
    try:
        answer, _ = _run_agent_loop(question, role=role, user_context=user_context)
        return answer
    except Exception as e:
        return f"Agent error: {e}"


# ── Interactive REPL ──────────────────────────────────────────────────────────

def interactive():
    """Interactive conversation loop with role selection."""
    print()
    print("=" * 60)
    print("  AGENTIC SOIL DIGITAL TWIN — Interactive Agent")
    print("=" * 60)
    print(f"  LLM: Ollama Cloud ({OLLAMA_MODEL})")
    print(f"  Soil type: {ACTIVE_SOIL_TYPE}")

    # Pre-flight connectivity check
    ok, msg = check_ollama_reachable()
    print(f"  Connectivity: {'✓ OK' if ok else '✗ ' + msg}")
    if not ok:
        print("\n  WARNING: Ollama Cloud is not reachable. Agent calls will fail.")
        print("  Please verify your internet connection and OLLAMA_BASE_URL/OLLAMA_API_KEY.")

    print()
    print("  Select your role:")
    print("    [1] Soil Scientist  (full technical detail)")
    print("    [2] Farmer          (simple actionable advice)")
    print()

    while True:
        choice = input("  Your role [1/2]: ").strip()
        if choice == "1":
            role = "scientist"
            role_label = "Soil Scientist"
            break
        elif choice == "2":
            role = "farmer"
            role_label = "Farmer"
            break
        else:
            print("  Please enter 1 or 2.")

    print()
    print(f"  Role: {role_label}")
    print("  Type your questions below. Commands:")
    print("    /role      — switch role")
    print("    /clear     — clear conversation history")
    print("    /diagnose  — run full depletion diagnosis")
    print("    /status    — show current sensor readings")
    print("    /help      — show this help")
    print("    /quit      — exit")
    print()

    history = []

    while True:
        try:
            question = input(f"  [{role_label}] You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Goodbye!")
            break

        if not question:
            continue

        if question.startswith("/"):
            cmd = question.lower().split()[0]

            if cmd == "/quit" or cmd == "/exit":
                print("\n  Goodbye!")
                break

            elif cmd == "/role":
                role = "farmer" if role == "scientist" else "scientist"
                role_label = "Farmer" if role == "farmer" else "Soil Scientist"
                history = []
                print(f"\n  Switched to: {role_label} (history cleared)\n")
                continue

            elif cmd == "/clear":
                history = []
                print("\n  Conversation history cleared.\n")
                continue

            elif cmd == "/diagnose":
                question = "Run a full soil depletion diagnosis."

            elif cmd == "/status":
                question = "Show me all current sensor readings."

            elif cmd == "/help":
                print("\n  Commands: /role, /clear, /diagnose, /status, /quit\n")
                continue

            else:
                print(f"\n  Unknown command: {cmd}\n")
                continue

        print()
        try:
            answer, history = _run_agent_loop(question, role=role, history=history)
            print(f"\n  ASDT: {answer}\n")
        except Exception as e:
            print(f"\n  Error: {e}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--ask":
        q = " ".join(sys.argv[2:])
        print(ask(q))
    else:
        interactive()