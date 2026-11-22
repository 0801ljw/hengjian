"""Small, closed JSON Schema vocabulary used by the AI controller (no coercion)."""
from .core import PolicyError


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 12000}
ID = {"type": "string", "minLength": 1, "maxLength": 64}


def array(items, minimum=0, maximum=16):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


SUBTASK = obj({"id": ID, "assignee": {"type": "string", "enum": ["gongbu"]},
               "instruction": TEXT, "depends_on": array(ID),
               "acceptance": array(TEXT, 1)})
ISSUE_ARTIFACT = obj({"path": TEXT, "content": TEXT})
ISSUE_SUBTASK = obj({"title": TEXT, "assignee": TEXT, "body": TEXT,
                     "acceptance": array(TEXT, 0, 8)})
ISSUE_RESULT = obj({
    "status": {"type": "string", "enum": ["done", "blocked", "delegate"]},
    "summary": TEXT,
    "blocker": {"type": "string", "minLength": 0, "maxLength": 4000},
    "artifacts": array(ISSUE_ARTIFACT, 0, 8),
    "subtasks": array(ISSUE_SUBTASK, 0, 4),
})
SCHEMAS = {
    "taizi": obj({"kind": {"type": "string", "enum": ["task", "chat"]},
                  "title": TEXT, "requirement": TEXT, "reason": TEXT}),
    "zhongshu": obj({"goal": TEXT, "subtasks": array(SUBTASK, 1, 4)}),
    "menxia": obj({"verdict": {"type": "string", "enum": ["approve", "reject"]},
                   "reasons": array(TEXT, 1), "required_changes": array(TEXT)}),
    "shangshu": obj({"order": array(ID, 1, 4), "reason": TEXT}),
    "gongbu": obj({"files": array(obj({"path": TEXT, "content": TEXT}), 1, 4),
                   "report": TEXT}),
    "issue_execute": ISSUE_RESULT,
}


def validate(value, schema, path="$", depth=0):
    if depth > 20:
        raise PolicyError("schema nesting limit")
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise PolicyError(f"schema {path}: missing or illegal fields")
        for key, child in value.items():
            validate(child, schema["properties"][key], f"{path}.{key}", depth + 1)
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise PolicyError(f"schema {path}: invalid array length/type")
        for i, child in enumerate(value):
            validate(child, schema["items"], f"{path}[{i}]", depth + 1)
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 12000):
            raise PolicyError(f"schema {path}: invalid string")
        if "enum" in schema and value not in schema["enum"]:
            raise PolicyError(f"schema {path}: illegal enum")
    else:
        raise PolicyError("unsupported schema type")


def check_plan(plan):
    validate(plan, SCHEMAS["zhongshu"])
    ids = [task["id"] for task in plan["subtasks"]]
    if len(set(ids)) != len(ids):
        raise PolicyError("duplicate subtask id")
    remaining = {task["id"]: set(task["depends_on"]) for task in plan["subtasks"]}
    if any(not deps <= set(ids) for deps in remaining.values()):
        raise PolicyError("unknown dependency")
    completed = set()
    while remaining:
        ready = {key for key, deps in remaining.items() if deps <= completed}
        if not ready:
            raise PolicyError("dependency cycle")
        completed |= ready
        remaining = {key: deps for key, deps in remaining.items() if key not in ready}


def check_order(order, plan):
    tasks = {task["id"]: task for task in plan["subtasks"]}
    if len(order) != len(tasks) or set(order) != set(tasks):
        raise PolicyError("dispatch must include every subtask exactly once")
    seen = set()
    for key in order:
        if not set(tasks[key]["depends_on"]) <= seen:
            raise PolicyError("dispatch violates dependency order")
        seen.add(key)
