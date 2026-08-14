#!/usr/bin/env python3
"""WordPress publishing backend for agent-journal.

Pushes a finished markdown entry into a WordPress site using wp-cli.
Idempotent: matches on slug and updates existing posts."""

import json
import subprocess
import sys
from pathlib import Path

from .writer import parse_frontmatter, slugify
from .site import md_to_html


def wp_run(config: dict, *args):
    wp_bin = config.get("wp_cli_path", "/usr/local/bin/wp")
    wp_path = Path(config["web_dir"]).resolve()
    cmd = [wp_bin, f"--path={wp_path}", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def get_post_id(config: dict, slug: str):
    res = wp_run(config, "post", "list", f"--name={slug}", "--post_type=post", "--format=ids")
    ids = res.stdout.strip().split()
    return int(ids[0]) if ids else None


def get_post_id_by_date(config: dict, date: str):
    """Return the newest published post whose post_date starts with `date`.

    Vision-mode journals publish once per day. If a retry generates a different
    slug, matching by date prevents duplicate posts.
    """
    res = wp_run(config, "post", "list", "--post_type=post", "--format=json",
                 "--posts_per_page=100")
    if res.returncode != 0:
        return None
    try:
        posts = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    prefix = date + " "
    for post in sorted(posts, key=lambda p: p.get("post_date", ""), reverse=True):
        if (post.get("post_date") or "").startswith(prefix):
            return int(post.get("ID"))
    return None


def publish_to_wordpress(entry_path: Path, config: dict) -> int:
    entry_path = Path(entry_path)
    text = entry_path.read_text(encoding="utf-8", errors="replace")
    fm, raw_body = parse_frontmatter(text)
    if not fm:
        print("ERROR: no frontmatter in entry", file=sys.stderr)
        return 1

    title = fm.get("title", "Untitled")
    date = fm.get("date", "")
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    category = fm.get("category", "")
    slug = slugify(title)
    body_html = md_to_html(raw_body.strip())

    content_file = entry_path.parent / f".wp-sync-{slug}.html"
    content_file.write_text(body_html, encoding="utf-8")

    # Prefer slug match (stable re-publishes), fall back to date match (retry
    # with a different title should update, not duplicate).
    post_id = get_post_id(config, slug)
    if not post_id and date:
        post_id = get_post_id_by_date(config, date)
    tags_arg = ",".join(tags)

    common_args = [
        f"--post_title={title}",
        f"--post_date={date} 08:00:00",
        f"--tags_input={tags_arg}",
    ]
    if category:
        common_args.append(f"--post_category={category}")

    if post_id:
        res = wp_run(
            config,
            "post", "update", str(post_id), str(content_file),
            f"--post_name={slug}",
            *common_args,
        )
        action = "updated"
    else:
        res = wp_run(
            config,
            "post", "create", str(content_file),
            f"--post_name={slug}",
            "--post_status=publish",
            "--post_author=1",
            *common_args,
            "--porcelain",
        )
        action = "created"

    content_file.unlink(missing_ok=True)

    if res.returncode != 0:
        print(f"ERROR: WordPress publish failed ({action}): {res.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"wordpress {action}: {slug} ({res.stdout.strip()})")
    return 0
