#!/usr/bin/env python3
"""Respond to unanswered replies on PR review comments using Claude."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
# Replies are conversational — 1024 tokens is plenty.
CLAUDE_MAX_TOKENS = int(os.environ.get("REPLY_MAX_TOKENS", "1024"))

REPLY_SYSTEM_PROMPT = """\
You are a code reviewer who posted inline comments on a pull request.
A developer has replied to one of your comments.

Your job:
- Read the full comment thread, including the code context shown in the diff hunk.
- Respond directly to the developer's last message.
- If they raise a valid point that changes your assessment, acknowledge it clearly and concisely.
- If your original concern still stands, explain it more concretely with a specific example or code path.
- Be concise, direct, and helpful. Avoid filler phrases.
- Do not re-review the whole PR. Stay focused on this thread only.
- Do not approve, merge, or comment on the overall PR status.
- Always respond in English.
"""


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh(*args: str) -> tuple[str | None, str | None]:
    """Run a gh CLI command. Returns (stdout, error_message)."""
    proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if proc.returncode == 0:
        return proc.stdout, None
    return None, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()


# ── Claude helper ──────────────────────────────────────────────────────────────

def call_claude(thread_prompt: str) -> str | None:
    """Call Claude and return the reply text, or None on failure."""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": REPLY_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": thread_prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            for block in result.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return (block.get("text") or "").strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"    Claude API error {exc.code}: {body[:300]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"    Claude API error: {exc}", file=sys.stderr)
    return None


# ── Thread helpers ─────────────────────────────────────────────────────────────

def root_id_of(comment: dict[str, Any], by_id: dict[int, Any]) -> int:
    """Walk up in_reply_to_id chain to find the root comment ID."""
    parent = comment.get("in_reply_to_id")
    if parent is None:
        return comment["id"]
    if parent in by_id:
        return root_id_of(by_id[parent], by_id)
    return parent  # root may be outside our fetched page


def build_thread_prompt(thread: list[dict[str, Any]], authed_user: str) -> str:
    """Format the thread as a prompt for Claude."""
    root = thread[0]
    lines: list[str] = [
        "# PR Review Comment Thread",
        "",
        f"**File:** {root.get('path', 'unknown')}",
        f"**Line:** {root.get('line') or root.get('original_line', '?')}",
    ]

    # Include the diff hunk so Claude has code context without re-fetching the PR.
    diff_hunk = (root.get("diff_hunk") or "").strip()
    if diff_hunk:
        lines += [
            "",
            "**Code context (diff hunk):**",
            "```diff",
            diff_hunk,
            "```",
        ]

    lines += ["", "## Conversation", ""]

    for comment in thread:
        author = (comment.get("user") or {}).get("login", "unknown")
        role = "**You (reviewer):**" if author == authed_user else f"**Developer (@{author}):**"
        body = (comment.get("body") or "").strip()
        # Truncate very long comment bodies to avoid blowing the context window.
        if len(body) > 2000:
            body = body[:2000] + "\n[... truncated]"
        lines += [role, body, ""]

    lines += [
        "---",
        "Reply to the developer's last message above. Be concise and specific.",
    ]
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: reply_review.py <repo_slug> <pr_number>", file=sys.stderr)
        return 2

    repo_slug, pr_number = sys.argv[1], sys.argv[2]

    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    # ── Get authenticated GitHub user ──────────────────────────────────────────
    user_raw, err = gh("api", "user", "--jq", ".login")
    if not user_raw:
        print(f"Could not get authenticated GitHub user: {err}", file=sys.stderr)
        return 1
    authed_user = user_raw.strip()
    print(f"Authenticated as: @{authed_user}")

    # ── Fetch PR review comments ───────────────────────────────────────────────
    print(f"Fetching review comments for {repo_slug} PR #{pr_number}...")
    raw, err = gh("api", f"repos/{repo_slug}/pulls/{pr_number}/comments?per_page=100")
    if raw is None:
        print(f"Could not fetch PR review comments: {err}", file=sys.stderr)
        return 1

    try:
        comments: list[dict[str, Any]] = json.loads(raw)
    except Exception as exc:
        print(f"Could not parse PR review comments: {exc}", file=sys.stderr)
        return 1

    if not isinstance(comments, list):
        print("Unexpected format for PR review comments.", file=sys.stderr)
        return 1

    print(f"Fetched {len(comments)} review comment(s).")

    # ── Build thread map ───────────────────────────────────────────────────────
    by_id: dict[int, dict[str, Any]] = {c["id"]: c for c in comments}

    threads: dict[int, list[dict[str, Any]]] = {}
    for comment in sorted(comments, key=lambda c: c.get("created_at", "")):
        rid = root_id_of(comment, by_id)
        threads.setdefault(rid, []).append(comment)

    # ── Find unanswered threads started by us ──────────────────────────────────
    # Criteria:
    #   1. Root comment was posted by the authenticated user (us)
    #   2. Last comment in the thread is NOT from us (user replied, awaiting answer)
    pending: list[tuple[int, list[dict[str, Any]]]] = []
    for root_id, thread in threads.items():
        root = by_id.get(root_id)
        if root is None:
            continue
        if (root.get("user") or {}).get("login") != authed_user:
            continue
        last = thread[-1]
        if (last.get("user") or {}).get("login") == authed_user:
            continue  # we already replied last — thread is current
        pending.append((root_id, thread))

    if not pending:
        print("No unanswered reply threads found.")
        return 0

    print(f"\nFound {len(pending)} unanswered thread(s).\n")

    replied = 0
    for root_id, thread in pending:
        root = by_id[root_id]
        path = root.get("path", "?")
        line = root.get("line") or root.get("original_line", "?")
        last_author = (thread[-1].get("user") or {}).get("login", "?")

        print(f"  Thread on {path}:{line}")
        print(f"    Last reply by: @{last_author}")

        prompt = build_thread_prompt(thread, authed_user)
        reply_text = call_claude(prompt)

        if reply_text is None:
            print("    ⚠ Claude failed to generate a reply — skipping.\n")
            continue

        # Post reply to the root comment of the thread via GitHub's reply endpoint.
        # NOTE: This ONLY posts a comment. It does NOT approve or merge the PR.
        post_raw, post_err = gh(
            "api",
            "--method", "POST",
            f"repos/{repo_slug}/pulls/{pr_number}/comments/{root_id}/replies",
            "--field", f"body={reply_text}",
        )
        if post_raw is None:
            print(f"    ⚠ Failed to post reply: {post_err}\n")
            continue

        print(f"    ✓ Reply posted.\n")
        replied += 1

    print(f"Replied to {replied}/{len(pending)} thread(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
