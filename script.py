"""
Reddit Mention Tracker → Slack
Monitors r/electricians, r/ibew, r/askelectricians for "dakota"
and posts to Slack whenever new mentions are found.

Setup:
  pip install praw requests

Environment variables (set these before running):
  REDDIT_CLIENT_ID       - from https://www.reddit.com/prefs/apps
  REDDIT_CLIENT_SECRET   - from https://www.reddit.com/prefs/apps
  REDDIT_USER_AGENT      - e.g. "DakotaPrepMentionBot/1.0"
  SLACK_WEBHOOK_URL      - from your Slack app's Incoming Webhooks config

Schedule: run every 2 hours via cron or GitHub Actions.
  Example cron: 0 */2 * * * /usr/bin/python3 /path/to/reddit_mention_tracker.py
"""

import os
import json
import time
import requests
import praw
from datetime import datetime, timezone, timedelta

# --- Config ---
KEYWORD = "dakota"
SUBREDDITS = ["electricians", "ibew", "askelectricians"]
LOOKBACK_HOURS = 3       # slightly over 2h to avoid missing posts near the boundary
SEEN_IDS_FILE  = os.path.join(os.path.dirname(__file__), ".seen_ids.json")
SEEN_IDS_MAX   = 5000     # cap file size; oldest entries are pruned beyond this
COUNTS_FILE    = os.path.join(os.path.dirname(__file__), ".mention_counts.json")

REDDIT_CLIENT_ID     = os.environ["REDDIT_CLIENT_ID"]
REDDIT_CLIENT_SECRET = os.environ["REDDIT_CLIENT_SECRET"]
REDDIT_USER_AGENT    = os.environ.get("REDDIT_USER_AGENT", "DakotaPrepMentionBot/1.0")
SLACK_WEBHOOK_URL    = os.environ["SLACK_WEBHOOK_URL"]


def load_seen_ids():
    """Load previously seen post/comment IDs from disk."""
    try:
        with open(SEEN_IDS_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen_ids(seen_ids):
    """Persist seen IDs to disk, pruning oldest if over the cap."""
    ids_list = list(seen_ids)
    if len(ids_list) > SEEN_IDS_MAX:
        ids_list = ids_list[-SEEN_IDS_MAX:]
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(ids_list, f)


def load_counts():
    """Load daily mention counts { 'YYYY-MM-DD': int } from disk."""
    try:
        with open(COUNTS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_counts(counts):
    """Persist daily counts, keeping only the last 400 days to cap file size."""
    keys = sorted(counts.keys())
    if len(keys) > 400:
        for old_key in keys[:-400]:
            del counts[old_key]
    with open(COUNTS_FILE, "w") as f:
        json.dump(counts, f, indent=2)


def record_counts(n):
    """Add n mentions to today's count and return updated counts dict."""
    counts = load_counts()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts[today] = counts.get(today, 0) + n
    save_counts(counts)
    return counts


def get_weekly_count(counts):
    """Sum mentions over the last 7 calendar days (including today)."""
    total = 0
    for i in range(7):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        total += counts.get(day, 0)
    return total


def get_monthly_count(counts):
    """Sum mentions for the current calendar month."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum(v for k, v in counts.items() if k.startswith(prefix))


def build_summary_message(counts, period):
    """Build a weekly or monthly Slack summary message."""
    assert period in ("weekly", "monthly")
    if period == "weekly":
        total = get_weekly_count(counts)
        label = "Weekly"
        emoji = "📅"
        since = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%b %d")
        through = datetime.now(timezone.utc).strftime("%b %d, %Y")
        timeframe = f"{since} – {through}"
    else:
        total = get_monthly_count(counts)
        label = "Monthly"
        emoji = "🗓️"
        timeframe = datetime.now(timezone.utc).strftime("%B %Y")

    subreddit_list = " • ".join(f"r/{s}" for s in SUBREDDITS)

    return {
        "text": f"{label} Reddit mention summary: {total} mention(s)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {label} Reddit Mention Summary"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Keyword:* \"{KEYWORD}\"\n"
                        f"*Subreddits:* {subreddit_list}\n"
                        f"*Period:* {timeframe}\n"
                        f"*Total mentions:* {total}"
                    )
                }
            }
        ]
    }


def get_reddit_client():
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )


def search_subreddit(reddit, subreddit_name, keyword, since_timestamp, seen_ids):
    """Search a subreddit for posts and comments containing the keyword.
    Skips anything already in seen_ids. Returns (mentions, new_ids)."""
    subreddit = reddit.subreddit(subreddit_name)
    mentions = []
    new_ids = set()

    # Search posts
    for post in subreddit.search(keyword, sort="new", time_filter="day", limit=100):
        if post.created_utc < since_timestamp:
            continue
        if post.id in seen_ids:
            continue
        if keyword.lower() in post.title.lower() or keyword.lower() in (post.selftext or "").lower():
            new_ids.add(post.id)
            mentions.append({
                "type": "Post",
                "subreddit": subreddit_name,
                "title": post.title,
                "url": f"https://reddit.com{post.permalink}",
                "author": str(post.author),
                "score": post.score,
                "created": datetime.fromtimestamp(post.created_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "snippet": (post.selftext[:200] + "...") if len(post.selftext) > 200 else post.selftext,
            })

    # Search comments
    for comment in subreddit.comments(limit=500):
        if comment.created_utc < since_timestamp:
            continue
        if comment.id in seen_ids:
            continue
        if keyword.lower() in comment.body.lower():
            new_ids.add(comment.id)
            snippet = comment.body[:200] + "..." if len(comment.body) > 200 else comment.body
            mentions.append({
                "type": "Comment",
                "subreddit": subreddit_name,
                "title": f"Comment by u/{comment.author}",
                "url": f"https://reddit.com{comment.permalink}",
                "author": str(comment.author),
                "score": comment.score,
                "created": datetime.fromtimestamp(comment.created_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "snippet": snippet,
            })

    return mentions, new_ids


def build_slack_message(mentions):
    """Build a Slack Block Kit message from the mention list."""
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    subreddit_list = " • ".join(f"r/{s}" for s in SUBREDDITS)

    if not mentions:
        return None  # caller should skip posting if nothing new

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔍 New Reddit Mentions: \"{KEYWORD}\""}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{now}* — {subreddit_list}\n*{len(mentions)} new mention(s)*"
            }
        },
        {"type": "divider"}
    ]

    for m in mentions:
        emoji = "📝" if m["type"] == "Post" else "💬"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{m['type']}* in r/{m['subreddit']} — <{m['url']}|{m['title']}>\n"
                    f"u/{m['author']} • {m['created']} • ⬆️ {m['score']}\n"
                    f"_{m['snippet']}_"
                )
            }
        })
        blocks.append({"type": "divider"})

    return {"text": f"New Reddit mentions of \"{KEYWORD}\"", "blocks": blocks}


def post_to_slack(message):
    response = requests.post(
        SLACK_WEBHOOK_URL,
        data=json.dumps(message),
        headers={"Content-Type": "application/json"}
    )
    if response.status_code != 200:
        raise ValueError(f"Slack webhook error {response.status_code}: {response.text}")
    print("✅ Posted to Slack successfully.")


def main():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).timestamp()
    reddit = get_reddit_client()
    seen_ids = load_seen_ids()

    all_mentions = []
    all_new_ids = set()

    for subreddit in SUBREDDITS:
        print(f"Searching r/{subreddit}...")
        try:
            mentions, new_ids = search_subreddit(reddit, subreddit, KEYWORD, since, seen_ids)
            print(f"  Found {len(mentions)} new mention(s)")
            all_mentions.extend(mentions)
            all_new_ids.update(new_ids)
        except Exception as e:
            print(f"  Error searching r/{subreddit}: {e}")
        time.sleep(1)  # be polite to the API

    # Always record counts (even zero) so days with no mentions are still logged
    counts = record_counts(len(all_mentions))

    if all_mentions:
        # Sort newest first and post mention alerts
        all_mentions.sort(key=lambda m: m["created"], reverse=True)
        message = build_slack_message(all_mentions)
        post_to_slack(message)

        # Only persist IDs after a successful Slack post
        seen_ids.update(all_new_ids)
        save_seen_ids(seen_ids)
        print(f"Saved {len(all_new_ids)} new ID(s) to seen list.")
    else:
        print("No new mentions. Skipping Slack post.")

    # Post weekly summary every Sunday at the first run of the day (00:00–01:59 UTC)
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() == 6 and now_utc.hour < 2:
        print("Posting weekly summary...")
        post_to_slack(build_summary_message(counts, "weekly"))

    # Post monthly summary on the 1st of each month at the first run of the day
    if now_utc.day == 1 and now_utc.hour < 2:
        print("Posting monthly summary...")
        post_to_slack(build_summary_message(counts, "monthly"))


if __name__ == "__main__":
    main()