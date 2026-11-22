"""Durable Issue queue with atomic claim, blockers, review gates and wakeups.

This is the Paperclip-style work layer for 衡鉴: an Issue is the unit of work,
not a model request. It deliberately does not execute an agent yet; the queue,
lease, blockers, decisions and wake records are the foundation the executor
will consume next.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone

from .core import PolicyError, now

STATES = ("queued", "blocked", "running", "review", "done", "cancelled")
OPEN_STATES = ("queued", "blocked", "running", "review")


def _id(prefix="ISS"):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _lease(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class IssueStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS issues(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL, parent_id TEXT, assignee TEXT NOT NULL,
                    model TEXT, reviewer TEXT, approver TEXT, launcher_id TEXT,
                    workspace_id TEXT NOT NULL DEFAULT 'w_legacy',
                    priority INTEGER NOT NULL DEFAULT 0,
                    acceptance TEXT NOT NULL, checks TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                    depth INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT,
                    claimed_by TEXT, claim_token TEXT,
                    lease_until TEXT, result TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS issue_blockers(
                    issue_id TEXT NOT NULL REFERENCES issues(id),
                    blocker_id TEXT NOT NULL REFERENCES issues(id),
                    PRIMARY KEY(issue_id, blocker_id));
                CREATE TABLE IF NOT EXISTS issue_comments(
                    seq INTEGER PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
                    at TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS issue_events(
                    seq INTEGER PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
                    at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS issue_wakeups(
                    id INTEGER PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
                    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, claimed_at TEXT, claimed_by TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS issue_wake_pending
                    ON issue_wakeups(issue_id, kind) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS issue_state ON issues(state, priority DESC, created_at);
            """)

        # Existing instances predate executable checks.
        columns = {row[1] for row in self.store.connect().execute("PRAGMA table_info(issues)")}
        with self.store.connect() as conn:
            if "checks" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN checks TEXT NOT NULL DEFAULT '[]'")
            if "attempts" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "max_attempts" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")
            if "depth" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
            if "retry_at" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN retry_at TEXT")
            if "max_tokens" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN max_tokens INTEGER NOT NULL DEFAULT 100000")
            if "max_calls" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN max_calls INTEGER NOT NULL DEFAULT 4")
            if "reserved_tokens" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN reserved_tokens INTEGER NOT NULL DEFAULT 0")
            if "max_cost_usd" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN max_cost_usd REAL")
            if "launcher_id" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN launcher_id TEXT")
            if "workspace_id" not in columns:
                conn.execute("ALTER TABLE issues ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'w_legacy'")

    def _event(self, conn, issue_id, actor, action, detail):
        conn.execute("INSERT INTO issue_events(issue_id,at,actor,action,detail) VALUES(?,?,?,?,?)",
                     (issue_id, now(), actor, action, json.dumps(detail, ensure_ascii=False)))

    def _wake(self, conn, issue_id, kind):
        conn.execute("INSERT OR IGNORE INTO issue_wakeups(issue_id,kind,created_at) VALUES(?,?,?)",
                     (issue_id, kind, now()))

    def _refresh_blockers(self, conn, issue_id):
        row = conn.execute("SELECT state FROM issues WHERE id=?", (issue_id,)).fetchone()
        if row is None or row[0] not in ("queued", "blocked"):
            return
        open_blockers = conn.execute("""
            SELECT 1 FROM issue_blockers b JOIN issues i ON i.id=b.blocker_id
            WHERE b.issue_id=? AND i.state NOT IN ('done','cancelled') LIMIT 1
        """, (issue_id,)).fetchone()
        target = "blocked" if open_blockers else "queued"
        if target != row[0]:
            conn.execute("UPDATE issues SET state=?,updated_at=? WHERE id=?", (target, now(), issue_id))
            self._event(conn, issue_id, "system", "state", {"to": target})
            if target == "queued":
                self._wake(conn, issue_id, "unblocked")

    def _refresh_dependents(self, conn, blocker_id):
        rows = conn.execute("SELECT issue_id FROM issue_blockers WHERE blocker_id=?",
                            (blocker_id,)).fetchall()
        for (issue_id,) in rows:
            self._refresh_blockers(conn, issue_id)

    def _root(self, conn, issue_id):
        current = issue_id
        seen = set()
        while current not in seen:
            seen.add(current)
            row = conn.execute("SELECT parent_id FROM issues WHERE id=?", (current,)).fetchone()
            if row is None or not row[0]:
                return current
            current = row[0]
        return issue_id

    def _tree_usage(self, conn, root_id):
        ids = []
        stack = [root_id]
        while stack:
            current = stack.pop()
            ids.append(current)
            stack.extend(row[0] for row in conn.execute(
                "SELECT id FROM issues WHERE parent_id=?", (current,)))
        total = 0
        for issue_id in ids:
            row = conn.execute("SELECT result FROM issues WHERE id=?", (issue_id,)).fetchone()
            if not row or not row[0]:
                continue
            try:
                usage = json.loads(row[0]).get("usage") or {}
            except (TypeError, ValueError):
                continue
            value = usage.get("total_tokens")
            if isinstance(value, int):
                total += value
        return total

    def _tree_reserved(self, conn, root_id):
        ids = []
        stack = [root_id]
        while stack:
            current = stack.pop()
            ids.append(current)
            stack.extend(row[0] for row in conn.execute(
                "SELECT id FROM issues WHERE parent_id=?", (current,)))
        return sum(int(row[0] or 0) for row in conn.execute(
            "SELECT reserved_tokens FROM issues WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)) if ids else 0

    def _monthly_usage(self, conn, assignee=None):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        sql = "SELECT result,reserved_tokens FROM issues WHERE substr(updated_at,1,7)=?"
        args = [month]
        if assignee is not None:
            sql += " AND assignee=?"
            args.append(assignee)
        total = 0
        for result, reserved in conn.execute(sql, args):
            total += int(reserved or 0)
            if not result:
                continue
            try:
                usage = json.loads(result).get("usage") or {}
            except (TypeError, ValueError):
                continue
            value = usage.get("total_tokens")
            if isinstance(value, int):
                total += value
        return total

    def _monthly_cost(self, conn, assignee=None):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        sql = "SELECT result FROM issues WHERE substr(updated_at,1,7)=?"
        args = [month]
        if assignee is not None:
            sql += " AND assignee=?"
            args.append(assignee)
        total = 0.0
        unknown = 0
        for (result,) in conn.execute(sql, args):
            if not result:
                continue
            try:
                usage = json.loads(result).get("usage") or {}
            except (TypeError, ValueError):
                continue
            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += float(cost)
            elif usage.get("pricing") == "unpriced":
                unknown += 1
        return total, unknown

    def _alert(self, issue_id, assignee, kind, title, body):
        try:
            from .alerts import raise_alert
            raise_alert(self.store.home, kind, assignee, title, body, ref=issue_id)
        except Exception:
            pass

    def _block_budget(self, conn, issue_id, reason, detail):
        conn.execute("""UPDATE issues SET state='blocked',result=?,updated_at=?,
                        revision=revision+1 WHERE id=?""",
                     (json.dumps({"error": reason, **detail}, ensure_ascii=False), now(), issue_id))
        self._event(conn, issue_id, "system", reason, detail)
        self._wake(conn, issue_id, "blocked")

    def create(self, *, title, assignee, body="", parent_id=None, blockers=(), reviewer=None,
               approver=None, priority=0, acceptance=(), checks=(), model=None, actor="human",
               max_attempts=3, max_tokens=100000, max_calls=4, max_cost_usd=None,
               launcher_id=None, workspace_id="w_legacy"):
        title = str(title or "").strip()
        assignee = str(assignee or "").strip()
        if not title or len(title) > 200:
            raise PolicyError("title is required and must be <= 200 chars")
        if not assignee or len(assignee) > 100:
            raise PolicyError("assignee is required")
        if not isinstance(workspace_id, str) or not workspace_id.startswith("w_") or len(workspace_id) > 80:
            raise PolicyError("invalid workspace")
        if launcher_id is not None:
            from .launchers import get
            launcher = get(self.store.home, str(launcher_id))
            if not launcher or launcher.get("workspace") != workspace_id:
                raise PolicyError("launcher not found in this workspace")
        if not isinstance(blockers, (list, tuple)) or len(blockers) > 32:
            raise PolicyError("blockers must be a list (max 32)")
        if not isinstance(acceptance, (list, tuple)) or len(acceptance) > 16:
            raise PolicyError("acceptance must be a list (max 16)")
        if any(not isinstance(item, str) or len(item) > 500 for item in acceptance):
            raise PolicyError("acceptance items must be strings <= 500 chars")
        if not isinstance(checks, (list, tuple)) or len(checks) > 16:
            raise PolicyError("checks must be a list (max 16)")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise PolicyError("max_attempts must be 1..10")
        if type(max_tokens) is not int or not 1000 <= max_tokens <= 10000000:
            raise PolicyError("max_tokens must be 1000..10000000")
        if type(max_calls) is not int or not 1 <= max_calls <= 8:
            raise PolicyError("max_calls must be 1..8")
        if any(not isinstance(item, dict) or set(item) - {"type", "path", "sha256", "contains"}
               for item in checks):
            raise PolicyError("invalid check shape")
        for item in checks:
            if item.get("type") not in ("file_exists", "sha256", "contains") or not isinstance(item.get("path"), str):
                raise PolicyError("check type/path is invalid")
            if item["type"] == "sha256" and not isinstance(item.get("sha256"), str):
                raise PolicyError("sha256 check requires sha256")
            if item["type"] == "contains" and not isinstance(item.get("contains"), str):
                raise PolicyError("contains check requires contains")
        if max_cost_usd is not None and (not isinstance(max_cost_usd, (int, float))
                                        or not 0 <= max_cost_usd <= 1_000_000):
            raise PolicyError("max_cost_usd must be 0..1000000")
        from .issue_policy import cap
        max_tokens, max_calls, max_cost_usd = cap(
            self.store.home, assignee, max_tokens, max_calls, max_cost_usd)
        issue_id = _id()
        with self.store.transaction() as conn:
            if parent_id is not None and conn.execute(
                    "SELECT 1 FROM issues WHERE id=?", (parent_id,)).fetchone() is None:
                raise PolicyError("parent issue not found")
            for blocker in blockers:
                if blocker == issue_id or conn.execute(
                        "SELECT 1 FROM issues WHERE id=?", (blocker,)).fetchone() is None:
                    raise PolicyError(f"blocker not found: {blocker}")
            state = "queued"
            if blockers:
                # The blocker rows are not inserted yet; ask each blocker directly.
                for blocker in blockers:
                    row = conn.execute("SELECT state FROM issues WHERE id=?", (blocker,)).fetchone()
                    if row and row[0] not in ("done", "cancelled"):
                        state = "blocked"
                        break
            conn.execute("""
                INSERT INTO issues(id,title,body,state,parent_id,assignee,model,reviewer,approver,
                    launcher_id,workspace_id,priority,acceptance,checks,attempts,max_attempts,max_tokens,max_calls,max_cost_usd,depth,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (issue_id, title, str(body or "")[:20000], state, parent_id, assignee, model,
                  reviewer, approver, launcher_id, workspace_id, int(priority), json.dumps(list(acceptance)),
                  json.dumps(list(checks)), 0, int(max_attempts), int(max_tokens), int(max_calls), max_cost_usd, 0, now(), now()))
            for blocker in blockers:
                conn.execute("INSERT INTO issue_blockers(issue_id,blocker_id) VALUES(?,?)",
                             (issue_id, blocker))
            self._event(conn, issue_id, actor, "created", {"assignee": assignee, "reviewer": reviewer,
                                                           "approver": approver, "launcher_id": launcher_id,
                                                           "blockers": list(blockers)})
            self._wake(conn, issue_id, "assigned")
        return self.get(issue_id)

    def get(self, issue_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise PolicyError("issue not found")
            columns = [item[0] for item in conn.execute("SELECT * FROM issues LIMIT 0").description]
            issue = dict(zip(columns, row))
            issue["acceptance"] = json.loads(issue["acceptance"])
            issue["checks"] = json.loads(issue["checks"] or "[]")
            issue["result"] = json.loads(issue["result"]) if issue["result"] else None
            issue["blockers"] = [item[0] for item in conn.execute(
                "SELECT blocker_id FROM issue_blockers WHERE issue_id=? ORDER BY blocker_id",
                (issue_id,))]
            issue["comments"] = [dict(zip(("seq", "at", "author", "body"), comment))
                                 for comment in conn.execute(
                                     "SELECT seq,at,author,body FROM issue_comments WHERE issue_id=? ORDER BY seq",
                                     (issue_id,))]
            issue["review_approved"] = any(
                json.loads(row[0]).get("verdict") == "approve"
                for row in conn.execute(
                    "SELECT detail FROM issue_events WHERE issue_id=? AND action=?",
                    (issue_id, "reviewed")))
            issue["children"] = [self.get(item[0]) for item in conn.execute(
                "SELECT id FROM issues WHERE parent_id=? ORDER BY created_at", (issue_id,))]
            return issue

    def list(self, state=None, workspace_id=None):
        if state is not None and state not in STATES:
            raise PolicyError("invalid state")
        with self.store.connect() as conn:
            sql = "SELECT id FROM issues"
            args = ()
            clauses = []
            if state:
                clauses.append("state=?")
                args += (state,)
            if workspace_id is not None:
                clauses.append("workspace_id=?")
                args += (workspace_id,)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY priority DESC, created_at"
            return [self.get(item[0]) for item in conn.execute(sql, args)]

    def comment(self, issue_id, author, body):
        text = str(body or "").strip()
        if not text or len(text) > 12000:
            raise PolicyError("comment is required and must be <= 12000 chars")
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone() is None:
                raise PolicyError("issue not found")
            conn.execute("INSERT INTO issue_comments(issue_id,at,author,body) VALUES(?,?,?,?)",
                         (issue_id, now(), str(author), text))
            conn.execute("UPDATE issues SET updated_at=? WHERE id=?", (now(), issue_id))
            self._event(conn, issue_id, author, "comment", {"body": text[:500]})
            self._wake(conn, issue_id, "commented")
        return self.get(issue_id)

    def claim(self, issue_id, worker, lease_seconds=60):
        if not worker or len(worker) > 100:
            raise PolicyError("worker is required")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state,retry_at FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise PolicyError("issue not found")
            if row[0] != "queued":
                raise PolicyError(f"issue is not claimable: {row[0]}")
            if row[1] and row[1] > now():
                raise PolicyError("retry is not due yet")
            if conn.execute("""
                SELECT 1 FROM issue_blockers b JOIN issues i ON i.id=b.blocker_id
                WHERE b.issue_id=? AND i.state NOT IN ('done','cancelled') LIMIT 1
            """, (issue_id,)).fetchone():
                conn.execute("UPDATE issues SET state='blocked',updated_at=? WHERE id=?", (now(), issue_id))
                raise PolicyError("issue is blocked")
            root_id = self._root(conn, issue_id)
            root_max = conn.execute("SELECT max_tokens FROM issues WHERE id=?", (root_id,)).fetchone()[0]
            used = self._tree_usage(conn, root_id)
            reserved = self._tree_reserved(conn, root_id)
            assignee = conn.execute("SELECT assignee FROM issues WHERE id=?", (issue_id,)).fetchone()[0]
            from .issue_policy import load as load_policy
            policy = load_policy(self.store.home)
            role_cap = int(policy.get("monthly_tokens_per_role", {}).get(assignee, 0) or 0)
            if role_cap:
                role_used = self._monthly_usage(conn, assignee)
                if role_used >= role_cap:
                    self._block_budget(conn, issue_id, "monthly role budget exhausted",
                                       {"assignee": assignee, "used": role_used, "cap": role_cap})
                    conn.commit()
                    self._alert(issue_id, assignee, "issue_budget_exhausted",
                                "Issue blocked by monthly role budget",
                                f"{assignee}: {role_used}/{role_cap}")
                    return self.get(issue_id)
            company_cap = int(policy.get("monthly_tokens", 0) or 0)
            if company_cap:
                company_used = self._monthly_usage(conn)
                if company_used >= company_cap:
                    self._block_budget(conn, issue_id, "monthly company budget exhausted",
                                       {"used": company_used, "cap": company_cap})
                    conn.commit()
                    self._alert(issue_id, assignee, "issue_budget_exhausted",
                                "Issue blocked by monthly company budget",
                                f"{company_used}/{company_cap}")
                    return self.get(issue_id)
            role_cost_cap = float(policy.get("monthly_cost_usd_per_role", {}).get(assignee, 0) or 0)
            if role_cost_cap:
                cost_used, unknown = self._monthly_cost(conn, assignee)
                if unknown or cost_used >= role_cost_cap:
                    self._block_budget(conn, issue_id, "monthly role cost budget exhausted",
                                       {"used": cost_used, "unknown": unknown, "cap": role_cost_cap})
                    conn.commit()
                    self._alert(issue_id, assignee, "issue_budget_exhausted",
                                "Issue blocked by monthly role cost",
                                f"{assignee}: {cost_used}/{role_cost_cap} unknown={unknown}")
                    return self.get(issue_id)
            company_cost_cap = float(policy.get("monthly_cost_usd", 0) or 0)
            if company_cost_cap:
                cost_used, unknown = self._monthly_cost(conn)
                if unknown or cost_used >= company_cost_cap:
                    self._block_budget(conn, issue_id, "monthly company cost budget exhausted",
                                       {"used": cost_used, "unknown": unknown, "cap": company_cost_cap})
                    conn.commit()
                    self._alert(issue_id, assignee, "issue_budget_exhausted",
                                "Issue blocked by monthly company cost",
                                f"{cost_used}/{company_cost_cap} unknown={unknown}")
                    return self.get(issue_id)
            remaining = max(0, int(root_max or 100000) - used - reserved)
            issue_max = conn.execute("SELECT max_tokens FROM issues WHERE id=?", (issue_id,)).fetchone()[0]
            if remaining < 1000:
                conn.execute("""UPDATE issues SET state='blocked',result=?,updated_at=?,
                                revision=revision+1 WHERE id=?""",
                             (json.dumps({"error": "root token budget exhausted"}, ensure_ascii=False),
                              now(), issue_id))
                self._event(conn, issue_id, "system", "budget_exhausted", {"used": used, "reserved": reserved})
                self._wake(conn, issue_id, "blocked")
                conn.commit()
                self._alert(issue_id, assignee, "issue_budget_exhausted",
                            "Issue blocked by root token budget", "root token budget exhausted")
                return self.get(issue_id)
            reserve = min(int(issue_max or 100000), max(1000, remaining // 2))
            token = uuid.uuid4().hex
            cursor = conn.execute("""UPDATE issues SET state='running',claimed_by=?,claim_token=?,lease_until=?,
                            attempts=attempts+1,retry_at=NULL,reserved_tokens=?,updated_at=?,revision=revision+1
                            WHERE id=? AND state='queued'""",
                         (worker, token, _lease(lease_seconds), reserve, now(), issue_id))
            if cursor.rowcount != 1:
                raise PolicyError("issue was claimed concurrently")
            conn.execute("UPDATE issue_wakeups SET status='done' WHERE issue_id=? AND status='pending'",
                         (issue_id,))
            self._event(conn, issue_id, worker, "claimed", {"lease_seconds": lease_seconds})
        issue = self.get(issue_id)
        issue["claim_token"] = token
        issue["budget_remaining"] = remaining
        issue["budget_reserved"] = reserve
        return issue

    def heartbeat(self, issue_id, claim_token, lease_seconds=60):
        with self.store.transaction() as conn:
            cur = conn.execute("""UPDATE issues SET lease_until=?,updated_at=?
                WHERE id=? AND state='running' AND claim_token=?""",
                               (_lease(lease_seconds), now(), issue_id, claim_token))
            if cur.rowcount != 1:
                raise PolicyError("claim is not active")
            self._event(conn, issue_id, "worker", "heartbeat", {"lease_seconds": lease_seconds})
        return self.get(issue_id)

    def recover_expired(self):
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT id,claimed_by FROM issues WHERE state='running' AND lease_until<?",
                                (now(),)).fetchall()
            for issue_id, claimed_by in rows:
                conn.execute("""UPDATE issues SET state='queued',claimed_by=NULL,claim_token=NULL,
                                lease_until=NULL,reserved_tokens=0,updated_at=?,revision=revision+1 WHERE id=?""",
                             (now(), issue_id))
                self._event(conn, issue_id, "system", "lease_expired", {"previous": claimed_by})
                self._wake(conn, issue_id, "recovered")
            return len(rows)

    def block(self, issue_id, blocker_id, actor="human"):
        if issue_id == blocker_id:
            raise PolicyError("issue cannot block itself")
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM issues WHERE id=?", (blocker_id,)).fetchone() is None:
                raise PolicyError("blocker not found")
            try:
                conn.execute("INSERT INTO issue_blockers(issue_id,blocker_id) VALUES(?,?)",
                             (issue_id, blocker_id))
            except Exception:
                raise PolicyError("blocker already exists") from None
            conn.execute("UPDATE issues SET state='blocked',updated_at=? WHERE id=?", (now(), issue_id))
            self._event(conn, issue_id, actor, "blocked", {"blocker_id": blocker_id})
        return self.get(issue_id)

    def unblock(self, issue_id, blocker_id, actor="human"):
        with self.store.transaction() as conn:
            cur = conn.execute("DELETE FROM issue_blockers WHERE issue_id=? AND blocker_id=?",
                               (issue_id, blocker_id))
            if cur.rowcount != 1:
                raise PolicyError("blocker not found")
            self._event(conn, issue_id, actor, "unblocked", {"blocker_id": blocker_id})
            self._refresh_blockers(conn, issue_id)
        return self.get(issue_id)

    def launcher_tasks(self, launcher_id):
        with self.store.connect() as conn:
            rows = conn.execute("""SELECT id FROM issues WHERE launcher_id=? AND state='queued'
                ORDER BY priority DESC, created_at LIMIT 50""", (launcher_id,)).fetchall()
        return [self.get(row[0]) for row in rows]

    def launcher_claim(self, issue_id, launcher_id):
        import secrets
        token = secrets.token_urlsafe(24)
        with self.store.transaction() as conn:
            row = conn.execute("""SELECT state,launcher_id FROM issues WHERE id=?""",
                               (issue_id,)).fetchone()
            if row is None or row[0] != "queued" or row[1] != launcher_id:
                raise PolicyError("issue is not queued for this launcher")
            conn.execute("""UPDATE issues SET state='running',claimed_by=?,claim_token=?,
                lease_until=?,updated_at=?,revision=revision+1 WHERE id=?""",
                         (f"launcher:{launcher_id}", token, _lease(600), now(), issue_id))
            self._event(conn, issue_id, f"launcher:{launcher_id}", "launcher_claimed",
                        {"launcher_id": launcher_id})
        return self.get(issue_id), token

    def launcher_heartbeat(self, issue_id, launcher_id, claim_token):
        with self.store.transaction() as conn:
            cur = conn.execute("""UPDATE issues SET lease_until=?,updated_at=?
                WHERE id=? AND launcher_id=? AND claim_token=? AND state='running'""",
                               (_lease(600), now(), issue_id, launcher_id, claim_token))
            if cur.rowcount != 1:
                raise PolicyError("launcher claim is not active")
        return True

    def launcher_complete(self, issue_id, launcher_id, claim_token, summary, artifacts=(), checks=(), usage=None):
        if not isinstance(artifacts, (list, tuple)) or len(artifacts) > 32:
            raise PolicyError("artifacts must be a list (max 32)")
        with self.store.transaction() as conn:
            row = conn.execute("""SELECT state,reviewer,approver FROM issues
                WHERE id=? AND claim_token=? AND launcher_id=?""",
                               (issue_id, claim_token, launcher_id)).fetchone()
            if row is None or row[0] != "running":
                raise PolicyError("launcher claim is not active")
            state = "review" if row[1] or row[2] else "done"
            conn.execute("""UPDATE issues SET state=?,claimed_by=NULL,claim_token=NULL,lease_until=NULL,
                reserved_tokens=0,retry_at=NULL,result=?,updated_at=?,revision=revision+1 WHERE id=?""",
                         (state, json.dumps({"summary": str(summary or "")[:12000],
                                             "artifacts": list(artifacts), "checks": list(checks),
                                             "usage": usage, "executed_by": f"launcher:{launcher_id}"},
                                            ensure_ascii=False), now(), issue_id))
            self._event(conn, issue_id, f"launcher:{launcher_id}", "launcher_completed",
                        {"state": state})
            if state == "review":
                self._wake(conn, issue_id, "review_requested")
            else:
                self._refresh_dependents(conn, issue_id)
        return self.get(issue_id)

    def launcher_fail(self, issue_id, launcher_id, claim_token, error, retry=True):
        issue = self.get(issue_id)
        if issue.get("launcher_id") != launcher_id:
            raise PolicyError("issue is not assigned to this launcher")
        return self.fail(issue_id, claim_token, error, retry=retry)

    def complete(self, issue_id, claim_token, summary, artifacts=(), checks=(), usage=None):
        if not isinstance(artifacts, (list, tuple)) or len(artifacts) > 32:
            raise PolicyError("artifacts must be a list (max 32)")
        with self.store.transaction() as conn:
            row = conn.execute("""SELECT state,reviewer,approver FROM issues
                WHERE id=? AND claim_token=?""", (issue_id, claim_token)).fetchone()
            if row is None or row[0] != "running":
                raise PolicyError("claim is not active")
            reviewer, approver = row[1], row[2]
            state = "review" if reviewer or approver else "done"
            conn.execute("""UPDATE issues SET state=?,claimed_by=NULL,claim_token=NULL,lease_until=NULL,
                            reserved_tokens=0,retry_at=NULL,result=?,updated_at=?,revision=revision+1 WHERE id=?""",
                         (state, json.dumps({"summary": str(summary or "")[:12000],
                                             "artifacts": list(artifacts),
                                             "checks": list(checks),
                                             "usage": usage}, ensure_ascii=False),
                          now(), issue_id))
            self._event(conn, issue_id, "worker", "completed", {"state": state})
            if state == "review":
                self._wake(conn, issue_id, "review_requested")
            else:
                self._refresh_dependents(conn, issue_id)
        return self.get(issue_id)

    def review(self, issue_id, actor, verdict, comment="", admin=False):
        if verdict not in ("approve", "reject"):
            raise PolicyError("verdict must be approve or reject")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state,reviewer,approver FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None or row[0] != "review":
                raise PolicyError("issue is not in review")
            if row[1] and actor != row[1] and not admin:
                raise PolicyError("only the assigned reviewer may review")
            if verdict == "approve":
                state = "review" if row[2] else "done"
            else:
                state = "queued"
            conn.execute("UPDATE issues SET state=?,updated_at=?,revision=revision+1 WHERE id=?",
                         (state, now(), issue_id))
            self._event(conn, issue_id, actor, "reviewed", {"verdict": verdict,
                                                            "comment": str(comment or "")[:2000],
                                                            "state": state})
            if verdict == "reject":
                self._wake(conn, issue_id, "changes_requested")
            elif state == "review":
                self._wake(conn, issue_id, "approval_requested")
            else:
                self._refresh_dependents(conn, issue_id)
        return self.get(issue_id)

    def approve(self, issue_id, actor, admin=False):
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state,approver,reviewer FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None or row[0] != "review":
                raise PolicyError("issue is not awaiting approval")
            if row[1] and actor != row[1] and not admin:
                raise PolicyError("only the assigned approver may approve")
            if row[2]:
                reviews = conn.execute(
                    "SELECT detail FROM issue_events WHERE issue_id=? AND action='reviewed'",
                    (issue_id,)).fetchall()
                if not any(json.loads(item[0]).get("verdict") == "approve" for item in reviews):
                    raise PolicyError("reviewer approval is required before sign-off")
            conn.execute("UPDATE issues SET state='done',updated_at=?,revision=revision+1 WHERE id=?",
                         (now(), issue_id))
            self._event(conn, issue_id, actor, "approved", {})
            self._refresh_dependents(conn, issue_id)
        return self.get(issue_id)

    def delegate(self, issue_id, claim_token, children):
        if not isinstance(children, list) or not 1 <= len(children) <= 4:
            raise PolicyError("delegate requires 1..4 child issues")
        with self.store.transaction() as conn:
            parent = conn.execute("""SELECT state,assignee,depth FROM issues
                WHERE id=? AND claim_token=?""", (issue_id, claim_token)).fetchone()
            if parent is None or parent[0] != "running":
                raise PolicyError("claim is not active")
            if parent[2] >= 2:
                raise PolicyError("delegation depth limit reached")
            from .agent_channels import CHANNELS
            from .cli_agents import AGENTS as CLI_AGENTS
            from .role_binding import ROLES
            allowed = set(ROLES) | set(CHANNELS) | set(CLI_AGENTS)
            override = os.environ.get("DUTANG_DELEGATION_ASSIGNEES", "").strip()
            if override:
                allowed = {item.strip() for item in override.split(",") if item.strip()}
            descendants = set()
            stack = [issue_id]
            while stack:
                current = stack.pop()
                for (child_id,) in conn.execute(
                        "SELECT id FROM issues WHERE parent_id=?", (current,)):
                    if child_id not in descendants:
                        descendants.add(child_id)
                        stack.append(child_id)
            if len(descendants) + len(children) > 8:
                raise PolicyError("delegation tree limit is 8 descendants")
            child_ids = []
            for child in children:
                if not isinstance(child, dict) or not isinstance(child.get("title"), str) \
                        or not isinstance(child.get("assignee"), str) or not child["title"].strip() \
                        or not child["assignee"].strip():
                    raise PolicyError("each child needs title and assignee")
                if len(child["title"]) > 200 or child["title"] == issue_id:
                    raise PolicyError("invalid child title")
                assignee = child["assignee"].strip()
                if assignee not in allowed and not assignee.startswith("custom-"):
                    raise PolicyError(f"assignee not allowed for delegation: {assignee}")
                acceptance = child.get("acceptance") or []
                if not isinstance(acceptance, list) or any(not isinstance(item, str) for item in acceptance):
                    raise PolicyError("child acceptance must be strings")
                from .issue_policy import cap
                child_tokens, child_calls, child_cost = cap(self.store.home, assignee, 100000, 4)
                cid = _id()
                conn.execute("""INSERT INTO issues(id,title,body,state,parent_id,assignee,model,
                    reviewer,approver,priority,acceptance,checks,attempts,max_attempts,max_tokens,max_calls,max_cost_usd,depth,
                    created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, child["title"], str(child.get("body") or "")[:20000], "queued",
                     issue_id, child["assignee"], None, None, None, 0,
                     json.dumps(acceptance), "[]", 0, 3, child_tokens, child_calls, child_cost, parent[2] + 1, now(), now()))
                conn.execute("INSERT INTO issue_blockers(issue_id,blocker_id) VALUES(?,?)",
                             (issue_id, cid))
                self._event(conn, cid, "worker", "created", {"parent_id": issue_id,
                                                             "assignee": child["assignee"]})
                self._wake(conn, cid, "assigned")
                child_ids.append(cid)
            conn.execute("""UPDATE issues SET state='blocked',claimed_by=NULL,claim_token=NULL,
                            lease_until=NULL,reserved_tokens=0,result=?,updated_at=?,revision=revision+1 WHERE id=?""",
                         (json.dumps({"delegated": child_ids}, ensure_ascii=False), now(), issue_id))
            self._event(conn, issue_id, "worker", "delegated", {"children": child_ids})
        return self.get(issue_id)

    def fail(self, issue_id, claim_token, error, retry=True):
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state,attempts,max_attempts,assignee FROM issues WHERE id=? AND claim_token=?",
                               (issue_id, claim_token)).fetchone()
            if row is None or row[0] != "running":
                raise PolicyError("claim is not active")
            retrying = bool(retry and row[1] < row[2])
            target = "queued" if retrying else "blocked"
            base = min(120, 5 * (2 ** max(0, row[1] - 1))) if retrying else 0
            jitter = int(hashlib.sha256(issue_id.encode()).hexdigest()[:4], 16) % 21
            delay = min(120, base + jitter) if retrying else None
            retry_at = _lease(delay) if delay else None
            conn.execute("""UPDATE issues SET state=?,claimed_by=NULL,claim_token=NULL,
                            lease_until=NULL,reserved_tokens=0,retry_at=?,result=?,updated_at=?,revision=revision+1 WHERE id=?""",
                         (target, retry_at, json.dumps({"error": str(error)[:4000], "attempts": row[1],
                                                        "will_retry": retrying, "retry_at": retry_at},
                                                       ensure_ascii=False), now(), issue_id))
            self._event(conn, issue_id, "worker", "retry" if retrying else "failed",
                        {"error": str(error)[:2000], "attempts": row[1]})
            self._wake(conn, issue_id, "retry" if retrying else "blocked")
        issue = self.get(issue_id)
        if not retrying:
            checks_failed = "acceptance checks failed" in str(error)
            kind = "issue_checks_failed" if checks_failed else "issue_retry_exhausted"
            title = "Issue acceptance checks failed" if checks_failed else "Issue retries exhausted"
            self._alert(issue_id, row[3], kind, title,
                        f"attempts={row[1]} error={str(error)[:500]}")
        return issue

    def cancel(self, issue_id, actor, reason=""):
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state FROM issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise PolicyError("issue not found")
            if row[0] in ("done", "cancelled"):
                raise PolicyError("issue is already terminal")
            conn.execute("""UPDATE issues SET state='cancelled',claimed_by=NULL,claim_token=NULL,
                            lease_until=NULL,reserved_tokens=0,updated_at=?,revision=revision+1 WHERE id=?""",
                         (now(), issue_id))
            self._event(conn, issue_id, actor, "cancelled", {"reason": str(reason or "")[:2000]})
            self._refresh_dependents(conn, issue_id)
        return self.get(issue_id)

    def enqueue_wake(self, issue_id, kind):
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone() is None:
                raise PolicyError("issue not found")
            self._wake(conn, issue_id, str(kind))

    def claim_wakeup(self, worker):
        with self.store.transaction() as conn:
            row = conn.execute("""SELECT w.id,w.issue_id,w.kind FROM issue_wakeups w
                JOIN issues i ON i.id=w.issue_id
                WHERE w.status='pending' ORDER BY i.priority DESC, w.id LIMIT 1""").fetchone()
            if row is None:
                return None
            cur = conn.execute("""UPDATE issue_wakeups SET status='claimed',claimed_at=?,claimed_by=?
                WHERE id=? AND status='pending'""", (now(), str(worker), row[0]))
            if cur.rowcount != 1:
                return None
            return {"id": row[0], "issue_id": row[1], "kind": row[2]}

    def finish_wakeup(self, wake_id, status="done"):
        if status not in ("done", "failed"):
            raise PolicyError("invalid wakeup status")
        with self.store.transaction() as conn:
            conn.execute("UPDATE issue_wakeups SET status=? WHERE id=? AND status='claimed'",
                         (status, wake_id))

    def wake_due_retries(self):
        with self.store.transaction() as conn:
            rows = conn.execute("""SELECT i.id FROM issues i WHERE i.state='queued'
                AND (i.retry_at IS NULL OR i.retry_at<=?)
                AND NOT EXISTS (SELECT 1 FROM issue_wakeups w
                                WHERE w.issue_id=i.id AND w.status='pending')""",
                (now(),)).fetchall()
            for (issue_id,) in rows:
                self._wake(conn, issue_id, "assigned")
            return len(rows)

    def pending_wakeups(self, limit=100):
        with self.store.connect() as conn:
            return [dict(zip(("id", "issue_id", "kind", "created_at"), row))
                    for row in conn.execute("""SELECT id,issue_id,kind,created_at FROM issue_wakeups
                        WHERE status='pending' ORDER BY id LIMIT ?""", (int(limit),))]
