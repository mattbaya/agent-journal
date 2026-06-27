#!/usr/bin/env python3
"""Static site generator for agent-journal.

Configurable via the config dict (site_title, site_tagline, site_url,
bot_email). Markdown -> HTML with a tiny in-house converter (no external
deps beyond stdlib). Adds the contact-email footer + per-entry comment
form. Comment form can be disabled by setting `enable_comment_form: false`.
"""
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path


DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

DEFAULT_TITLE = "Agent journal"
DEFAULT_TAGLINE = "Daily notes from an AI agent."


def canonical_date_for(entry_path, frontmatter):
    m = DATE_RE.match(Path(entry_path).name)
    if m:
        return m.group(1)
    return frontmatter.get("date", "")


def md_to_html(md: str) -> str:
    blocks = []

    def stash_codeblock(m):
        lang = m.group(1) or ""
        code = m.group(2)
        blocks.append(f'<pre><code class="lang-{lang}">{html.escape(code)}</code></pre>')
        return f"\x00BLOCK{len(blocks)-1}\x00"

    md = re.sub(r"```(\w*)\n(.*?)\n```", stash_codeblock, md, flags=re.S)
    out_lines = []
    for para in re.split(r"\n\s*\n", md):
        para = para.strip("\n")
        if not para:
            continue
        if re.fullmatch(r"\x00BLOCK\d+\x00", para):
            out_lines.append(blocks[int(re.search(r"\d+", para).group())])
            continue
        h = re.match(r"^(#{1,6})\s+(.+)$", para)
        if h and "\n" not in para:
            level = len(h.group(1))
            out_lines.append(f"<h{level}>{inline(h.group(2))}</h{level}>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", para):
            out_lines.append("<hr>")
            continue
        if all(line.startswith(">") for line in para.splitlines()):
            inner = "\n".join(line.lstrip(">").lstrip() for line in para.splitlines())
            out_lines.append("<blockquote>" + inline(inner).replace("\n", "<br>") + "</blockquote>")
            continue
        if all(re.match(r"^[-*]\s+", line) for line in para.splitlines()):
            items = "".join(f"<li>{inline(line[2:].strip())}</li>" for line in para.splitlines())
            out_lines.append(f"<ul>{items}</ul>")
            continue
        if all(re.match(r"^\d+\.\s+", line) for line in para.splitlines()):
            items = "".join(f'<li>{inline(re.sub(r"^\\d+\\.\\s+", "", line))}</li>'
                            for line in para.splitlines())
            out_lines.append(f"<ol>{items}</ol>")
            continue
        text = inline(para).replace("\n", "<br>\n")
        out_lines.append(f"<p>{text}</p>")

    html_out = "\n\n".join(out_lines)
    for i, b in enumerate(blocks):
        html_out = html_out.replace(f"\x00BLOCK{i}\x00", b)
    return html_out


def _linkify_bare(m):
    """Wrap a bare http(s) URL in <a>, keeping trailing punctuation outside the link."""
    url = m.group(1)
    trail = ""
    while url and url[-1] in ".,;:!?)]}'\"":
        trail = url[-1] + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # auto-link bare URLs (skip those already inside an href= or as link text right after >)
    text = re.sub(r'(?<![">])(https?://[^\s<>"]+)', _linkify_bare, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\*_])\*([^*]+)\*(?![\*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\*_])_([^_]+)_(?![_])", r"<em>\1</em>", text)
    return text


def parse_frontmatter(text: str):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
    body = text[m.end():]
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            try:
                fm[k.strip()] = json.loads(v)
            except Exception:
                fm[k.strip()] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        elif v.startswith('"') and v.endswith('"'):
            try:
                fm[k.strip()] = json.loads(v)
            except Exception:
                fm[k.strip()] = v.strip('"')
        else:
            fm[k.strip()] = v
    return fm, body


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {site_title}</title>
<link rel="stylesheet" href="/style.css">
<link rel="alternate" type="application/rss+xml" title="{site_title}" href="/feed.xml">
</head>
<body>
<header>
  <h1><a href="/">{site_title}</a></h1>
  <p class="tagline">{site_tagline}</p>
</header>
<main>
{body}
</main>
<footer>
  <p>Written by {bot_name}, an AI agent, on {date}.
  <a href="/">All entries</a> · <a href="/feed.xml">RSS</a></p>
  {email_footer}
</footer>
</body>
</html>
"""

STYLE = """:root { --ink: #1a1a1a; --paper: #fafaf6; --accent: #5a4a3a; --rule: #d8d4ca; }
* { box-sizing: border-box; }
body { background: var(--paper); color: var(--ink); font: 17px/1.55 Georgia, 'Iowan Old Style', 'Charter', serif; max-width: 38em; margin: 0 auto; padding: 2em 1.25em 4em; }
header { border-bottom: 1px solid var(--rule); padding-bottom: 1em; margin-bottom: 2em; }
h1 { font-size: 1.45em; margin: 0 0 .1em; font-weight: 600; letter-spacing: -.01em; }
h1 a { color: var(--ink); text-decoration: none; }
.tagline { color: var(--accent); font-style: italic; font-size: .95em; margin: 0; }
h2, h3 { letter-spacing: -.01em; line-height: 1.25; }
article + article { border-top: 1px solid var(--rule); margin-top: 2.5em; padding-top: 2em; }
article header { border: none; margin-bottom: .8em; padding: 0; }
article h2 { margin: 0 0 .15em; font-size: 1.3em; }
article h2 a { color: var(--ink); text-decoration: none; }
.meta { color: var(--accent); font-size: .85em; font-style: italic; }
.tags a { color: var(--accent); text-decoration: none; }
.tags a:hover { text-decoration: underline; }
.summary { color: var(--accent); margin: .25em 0 .8em; font-style: italic; }
blockquote { border-left: 3px solid var(--accent); margin: 1em 0; padding: .3em 0 .3em 1em; color: #444; font-style: italic; }
pre { background: #f0ece2; padding: 1em; overflow-x: auto; border-radius: 3px; font-size: .9em; }
code { font-family: 'SF Mono', 'Menlo', monospace; font-size: .92em; }
:not(pre) > code { background: #f0ece2; padding: 1px 4px; border-radius: 2px; }
a { color: #6b4a2a; text-decoration: underline; text-decoration-thickness: .5px; text-underline-offset: 2px; }
a:hover { color: var(--ink); }
hr { border: none; border-top: 1px solid var(--rule); margin: 2em 0; }
footer { margin-top: 4em; padding-top: 1em; border-top: 1px solid var(--rule); color: var(--accent); font-size: .85em; font-style: italic; }
.comment-form { margin-top: 3em; padding-top: 2em; border-top: 1px solid var(--rule); }
.comment-form h3 { margin: 0 0 .5em; font-size: 1.1em; }
.comment-form p { margin: .6em 0; }
.comment-form label { display: block; color: var(--accent); font-size: .9em; }
.comment-form input[type="text"], .comment-form input[type="email"], .comment-form textarea {
  display: block; width: 100%; padding: .5em; border: 1px solid var(--rule); border-radius: 3px;
  font: inherit; font-size: .95em; background: #fff;
}
.comment-form textarea { font-family: 'SF Mono', 'Menlo', monospace; font-size: .9em; resize: vertical; }
.comment-form button { padding: .5em 1.5em; border: 1px solid var(--accent); background: var(--paper);
  color: var(--ink); font: inherit; cursor: pointer; border-radius: 3px; }
.comment-form button:hover { background: var(--accent); color: var(--paper); }
"""


def _email_footer(config: dict) -> str:
    addr = config.get("bot_email", "")
    if not addr:
        return ""
    tmpl = config.get(
        "email_footer_text",
        'Or just write: <a href="mailto:{addr}">{addr}</a>. '
        "Replies are also cc'd to a human who reviews the output.",
    )
    return "<p>" + tmpl.format(addr=html.escape(addr)) + "</p>"


def _comment_form(slug: str, config: dict, bot_name: str) -> str:
    if not config.get("enable_comment_form", True):
        return ""
    cgi_path = config.get("comment_cgi_path", "/cgi-bin/comment.cgi")
    intro = config.get(
        "comment_intro_text",
        "Comments go to the inbox and are read as part of the daily writing.\n"
        "A reply may or may not come back. Replies are cc'd to a human who\n"
        "reviews the output.",
    )
    # Cloudflare Turnstile widget — rendered only when a (public) site key is
    # configured. The widget injects a hidden `cf-turnstile-response` field
    # into the form, which comment.cgi verifies server-side. Leave
    # `turnstile_sitekey` empty/unset to disable (current behavior).
    sitekey = config.get("turnstile_sitekey", "")
    captcha = (
        f'  <p><div class="cf-turnstile" data-sitekey="{html.escape(sitekey)}"></div></p>\n'
        '  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>\n'
        if sitekey else ""
    )
    return f'''<section class="comment-form">
<h3>Write to {html.escape(bot_name)}</h3>
<p>{html.escape(intro)}</p>
<form method="post" action="{html.escape(cgi_path)}">
  <input type="hidden" name="entry" value="{html.escape(slug)}">
  <div aria-hidden="true" style="position:absolute;left:-9999px">
    <label>Website (leave empty): <input type="text" name="website" autocomplete="off" tabindex="-1"></label>
  </div>
  <p><label>Name (optional)<br><input type="text" name="name" maxlength="100"></label></p>
  <p><label>Email (required, so {html.escape(bot_name)} could reply)<br>
    <input type="email" name="email" required maxlength="200"></label></p>
  <p><label>Comment<br>
    <textarea name="comment" required maxlength="4000" rows="8"></textarea></label></p>
{captcha}  <p><button type="submit">Send</button></p>
</form>
</section>'''


def render_tags(tags):
    if not tags:
        return ""
    return " ".join(f'<a href="/?tag={html.escape(t)}">#{html.escape(t)}</a>' for t in tags)


def render_entry_page(fm, body_html, slug, config):
    site_title = config.get("site_title", DEFAULT_TITLE)
    site_tagline = config.get("site_tagline", DEFAULT_TAGLINE)
    bot_name = config.get("display_name") or config.get("bot_name", "the agent")
    title = fm.get("title", "Untitled")
    date = fm.get("date", "")
    tags = fm.get("tags") or []
    summary = fm.get("summary", "")
    article = f'''<article>
<header>
  <h2>{html.escape(title)}</h2>
  <div class="meta">{date} ·
    <span class="tags">{render_tags(tags)}</span>
  </div>
  {f'<p class="summary">{html.escape(summary)}</p>' if summary else ''}
</header>
{body_html}
</article>
{_comment_form(slug or "index", config, bot_name)}'''
    return PAGE.format(
        title=html.escape(title),
        site_title=html.escape(site_title),
        site_tagline=html.escape(site_tagline),
        bot_name=html.escape(bot_name),
        date=date,
        body=article,
        email_footer=_email_footer(config),
    )


def render_index(entries, config):
    site_title = config.get("site_title", DEFAULT_TITLE)
    site_tagline = config.get("site_tagline", DEFAULT_TAGLINE)
    bot_name = config.get("display_name") or config.get("bot_name", "the agent")
    if not entries:
        body = "<p><em>No entries yet.</em></p>"
    else:
        items = []
        for e in entries:
            slug, date = e["slug"], e["date"]
            url = f"/{date}-{slug}.html"
            summary_html = f'<p class="summary">{html.escape(e.get("summary", ""))}</p>' if e.get("summary") else ""
            items.append(
                f'<article>\n'
                f'<header>\n'
                f'  <h2><a href="{url}">{html.escape(e["title"])}</a></h2>\n'
                f'  <div class="meta">{date} ·\n'
                f'    <span class="tags">{render_tags(e.get("tags") or [])}</span>\n'
                f'  </div>\n'
                f'  {summary_html}\n'
                f'</header>\n'
                f'</article>'
            )
        body = "\n".join(items)
    return PAGE.format(
        title=html.escape(site_title),
        site_title=html.escape(site_title),
        site_tagline=html.escape(site_tagline),
        bot_name=html.escape(bot_name),
        date=datetime.now().strftime("%Y-%m-%d"),
        body=body,
        email_footer=_email_footer(config),
    )


def render_feed(entries, config):
    site_title = config.get("site_title", DEFAULT_TITLE)
    site_tagline = config.get("site_tagline", DEFAULT_TAGLINE)
    site_url = config.get("site_url", "")
    items = []
    for e in entries[:30]:
        url = f"{site_url}/{e['date']}-{e['slug']}.html"
        try:
            pub = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            pub_rfc = pub.strftime("%a, %d %b %Y 00:00:00 +0000")
        except ValueError:
            pub_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        items.append(
            f"<item>\n"
            f"<title>{html.escape(e['title'])}</title>\n"
            f"<link>{url}</link>\n"
            f"<guid>{url}</guid>\n"
            f"<pubDate>{pub_rfc}</pubDate>\n"
            f"<description>{html.escape(e.get('summary', ''))}</description>\n"
            f"</item>"
        )
    now_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n<channel>\n'
        f'<title>{html.escape(site_title)}</title>\n'
        f'<link>{site_url}/</link>\n'
        f'<description>{html.escape(site_tagline)}</description>\n'
        f"<language>en</language>\n<lastBuildDate>{now_rfc}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )


def render_sitemap(entries: list, config: dict) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [
        f"  <url>\n"
        f"    <loc>{site_url}/</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>daily</changefreq>\n"
        f"    <priority>1.0</priority>\n"
        f"  </url>"
    ]
    for e in entries:
        loc = f"{site_url}/{e['date']}-{e['slug']}.html"
        urls.append(
            f"  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{e['date']}</lastmod>\n"
            f"    <changefreq>never</changefreq>\n"
            f"    <priority>0.8</priority>\n"
            f"  </url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


def render_robots(config: dict) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )


def build_site(published_dir: Path, site_dir: Path, index: list, config: dict) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "style.css").write_text(STYLE)
    for md_path in Path(published_dir).glob("*.md"):
        text = md_path.read_text()
        fm, body = parse_frontmatter(text)
        fm["date"] = canonical_date_for(md_path, fm)
        body_html = md_to_html(body.strip())
        (site_dir / (md_path.stem + ".html")).write_text(
            render_entry_page(fm, body_html, md_path.stem, config)
        )
    (site_dir / "index.html").write_text(render_index(index, config))
    (site_dir / "feed.xml").write_text(render_feed(index, config))
    (site_dir / "sitemap.xml").write_text(render_sitemap(index, config))
    (site_dir / "robots.txt").write_text(render_robots(config))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    config = json.loads(Path(args.config).read_text())
    journal_dir = Path(config["journal_dir"]).resolve()
    site_dir = Path(config["web_dir"]).resolve()
    idx_path = journal_dir / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else []
    build_site(journal_dir / "published", site_dir, index, config)
    print(f"site built at {site_dir}")
