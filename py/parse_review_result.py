#!/usr/bin/env python3
"""Parse Anthropic Messages API output into GitHub review artifacts."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ACCEPTED_BODY = os.environ.get(
    "ACCEPTED_REVIEW_BODY",
    "Review aceita. Nenhum problema bloqueante encontrado.",
)
COMMENTS_BODY = os.environ.get("COMMENTS_REVIEW_BODY", "Claude inline review.")
CONFIDENCE_THRESHOLD = int(os.environ.get("CONFIDENCE_THRESHOLD", "70"))


def read_json(path: str) -> tuple[dict[str, Any], str | None, str]:
    raw = ""
    if os.path.exists(path):
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            return data, None, raw
        return {}, "TOP_LEVEL_JSON_NOT_OBJECT", raw
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc), raw


def extract_anthropic_text(data: dict[str, Any], raw: str) -> str:
    """Support Anthropic Messages API and older/CLI-like response shapes."""
    content = data.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n".join(parts).strip()

    for key in ("result", "response", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return raw.strip()


def extract_json_object(text: str) -> tuple[dict[str, Any], str | None]:
    """Extract the review JSON from Claude's response.

    Tries multiple strategies so a stray preamble/postamble never silently
    causes the review to be treated as "accepted".
    """
    text = (text or "").strip()
    if not text:
        return {"status": "accepted", "comments": []}, "EMPTY_RESULT"

    def _try_parse(s: str) -> dict[str, Any] | None:
        try:
            value = json.loads(s)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    # Strategy 1: fenced code block (```json ... ``` or ``` ... ```)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced:
        result = _try_parse(fenced.group(1).strip())
        if result is not None:
            return result, None

    # Strategy 2: bracket-counting — find every top-level {...} in the text,
    # prefer the largest one that has a "status" key (our review JSON).
    candidates: list[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(text[start : i + 1])
                start = -1

    for candidate in sorted(candidates, key=len, reverse=True):
        result = _try_parse(candidate)
        if result is not None and "status" in result:
            return result, None

    # Strategy 3: parse the whole stripped text as-is
    result = _try_parse(text)
    if result is not None:
        return result, None

    return {"status": "accepted", "comments": []}, f"COMMENT_JSON_PARSE_FAILED: no valid JSON object found in response (length={len(text)})"


def main() -> int:
    if len(sys.argv) != 12:
        print(
            "Usage: parse_review_result.py <claude_json> <review_md> <usage_txt> "
            "<review_input> <claude_exit> <targets_json> <inline_comments_json> "
            "<payload_json> <pr_json> <count_txt> <status_txt>",
            file=sys.stderr,
        )
        return 2

    (
        claude_json_path,
        review_path,
        usage_path,
        review_input_path,
        claude_exit_raw,
        targets_path,
        inline_comments_path,
        inline_payload_path,
        pr_json_path,
        count_path,
        status_path,
    ) = sys.argv[1:]

    claude_exit = int(claude_exit_raw)
    data, parse_error, raw = read_json(claude_json_path)
    review_text = extract_anthropic_text(data, raw)
    comments_json, comment_parse_error = extract_json_object(review_text)

    with open(targets_path, "r", encoding="utf-8") as f:
        targets = json.load(f)

    valid_targets = {
        (str(t.get("path")), int(t.get("line")), str(t.get("side")))
        for t in targets
        if t.get("path") and t.get("line") is not None and t.get("side")
    }

    valid_comments: list[dict[str, Any]] = []
    discarded_comments: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, int, str]] = set()

    for item in comments_json.get("comments") or []:
        if not isinstance(item, dict):
            discarded_comments.append({"reason": "comment_not_object", "comment": item})
            continue

        path = str(item.get("path") or "").strip()
        side = str(item.get("side") or "RIGHT").strip().upper()
        body = str(item.get("body") or "").strip()

        # Parse confidence as integer (supports both numeric and legacy string values)
        try:
            confidence_value = int(float(str(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence_value = 0

        try:
            line = int(item.get("line"))
        except Exception:  # noqa: BLE001
            discarded_comments.append({"reason": "invalid_line", "comment": item})
            continue

        if not path or not body:
            discarded_comments.append({"reason": "missing_path_or_body", "comment": item})
            continue
        if side not in {"LEFT", "RIGHT"}:
            discarded_comments.append({"reason": "invalid_side", "comment": item})
            continue
        if confidence_value < CONFIDENCE_THRESHOLD:
            discarded_comments.append({
                "reason": f"low_confidence ({confidence_value} < {CONFIDENCE_THRESHOLD})",
                "comment": item,
            })
            continue
        if (path, line, side) not in valid_targets:
            discarded_comments.append({"reason": "target_not_in_diff", "comment": item})
            continue
        if (path, line, side) in seen_targets:
            discarded_comments.append({"reason": "duplicate_target", "comment": item})
            continue

        seen_targets.add((path, line, side))
        valid_comments.append({
            "path": path,
            "line": line,
            "side": side,
            "body": body,
            "confidence": confidence_value,
            "severity": str(item.get("severity") or "").strip(),
        })

    status = "comments" if valid_comments else "accepted"
    normalized: dict[str, Any] = {"status": status, "comments": valid_comments}
    if discarded_comments:
        normalized["discarded_comments"] = discarded_comments
    if comment_parse_error:
        normalized["parse_warning"] = comment_parse_error

    Path(inline_comments_path).write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with open(pr_json_path, "r", encoding="utf-8") as f:
        pr_data = json.load(f)

    head_sha = pr_data.get("headRefOid") or ""
    review_body = COMMENTS_BODY if valid_comments else ACCEPTED_BODY
    payload = {"commit_id": head_sha, "event": "COMMENT", "body": review_body}
    if valid_comments:
        # GitHub API only accepts path/line/side/body — strip display-only fields
        payload["comments"] = [
            {"path": c["path"], "line": c["line"], "side": c["side"], "body": c["body"]}
            for c in valid_comments
        ]

    Path(inline_payload_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path(count_path).write_text(str(len(valid_comments)), encoding="utf-8")
    Path(status_path).write_text(status, encoding="utf-8")

    low_confidence_count = sum(
        1 for d in discarded_comments
        if str(d.get("reason", "")).startswith("low_confidence")
    )

    if valid_comments:
        lines = ["# Inline PR review comments preview", ""]
        for idx, comment in enumerate(valid_comments, start=1):
            confidence = comment.get("confidence") or "N/A"
            severity = comment.get("severity") or "N/A"
            lines += [
                f"### Comment {idx}",
                f"**File:** {comment['path']}",
                f"**Line:** {comment['line']}",
                f"**Side:** {comment['side']}",
                f"**Severity:** {severity}",
                f"**Confidence:** {confidence}",
                "",
                comment["body"],
                "",
            ]
        if discarded_comments:
            lines += [
                "## Discarded comments",
                "",
                "Some Claude-generated comments were discarded because their target was not present in the diff target map.",
            ]
    else:
        lines = [ACCEPTED_BODY]
        # Surface any diagnostic info so the user knows why no comments were posted
        if comment_parse_error:
            lines += [
                "",
                f"⚠️  JSON parse warning: {comment_parse_error}",
                "    The raw Claude response has been saved to the usage file.",
                "    Set OUTPUT_DIR=<path> to persist it between runs.",
            ]
        elif low_confidence_count:
            lines += [
                "",
                f"ℹ️  {low_confidence_count} comment(s) were generated but discarded",
                f"   because confidence was below the threshold ({CONFIDENCE_THRESHOLD}/100).",
                "    Lower CONFIDENCE_THRESHOLD to see them.",
            ]

    Path(review_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    input_bytes = os.path.getsize(review_input_path)
    approx_input_tokens = input_bytes // 4
    usage = data.get("usage") if isinstance(data, dict) else None

    with open(usage_path, "w", encoding="utf-8") as f:
        f.write("# Claude Review Usage\n\n")
        f.write(f"- Claude exit code: {claude_exit}\n")
        f.write(f"- Claude JSON parse status: {'FAILED: ' + parse_error if parse_error else 'OK'}\n")
        f.write(f"- Inline comment JSON parse status: {'FAILED: ' + comment_parse_error if comment_parse_error else 'OK'}\n")
        f.write(f"- Review status: {status}\n")
        f.write(f"- Confidence threshold: {CONFIDENCE_THRESHOLD}/100\n")
        f.write(f"- Valid inline comments: {len(valid_comments)}\n")
        f.write(f"- Discarded (low confidence): {low_confidence_count}\n")
        f.write(f"- Discarded (other reasons): {len(discarded_comments) - low_confidence_count}\n")
        f.write(f"- Claude result subtype: {data.get('subtype', 'not reported')}\n")
        f.write(f"- Claude is_error: {data.get('is_error', 'not reported')}\n")
        f.write(f"- Model: {data.get('model', 'not reported')}\n")
        f.write(f"- Stop reason: {data.get('stop_reason', 'not reported')}\n")
        f.write("\n## Input size\n\n")
        f.write(f"- Review input bytes: {input_bytes}\n")
        f.write(f"- Approx input tokens: {approx_input_tokens}\n")
        f.write("\n## Token usage reported by Claude\n\n")
        if isinstance(usage, dict):
            for key, value in usage.items():
                f.write(f"- {key}: {value}\n")
        else:
            f.write("- usage: not reported by this API output\n")
        # Always save the raw response so failures can be diagnosed
        f.write("\n## Raw Claude response (first 3000 chars)\n\n")
        f.write("```\n")
        f.write(review_text[:3000] if review_text else "(empty)")
        f.write("\n```\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
