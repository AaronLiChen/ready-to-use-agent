#!/usr/bin/env python3
"""Read-only Teams review-request intake through the configured silent-reader contract."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == '__main__':sys.modules.setdefault('teams_intake', sys.modules[__name__])
import workflow as w

WORKIQ = Path(os.environ.get('AUTOWORKFLOW_TEAMS_READER', os.environ.get('AUTOWORKFLOW_WORKIQ', str(w.CODE / 'bin/teams-reader'))))
_ACTIVE_READS = []
_ACTIVE_LOCK = threading.RLock()
REVIEW = re.compile(r"\bptal\b|\bre-?review\b|(?:please|pls|could you|can you|would you|help|need|request|asking|ready for).{0,100}(?:review|approve|take (?:a )?look)|(?:review|approve|take a look)\s+(?:(?:this|my|our|the)\b|https?://)|(?:review|approve|take a look).{0,60}\b(?:please|pls)\b|(?:帮忙|请).{0,30}(?:看|review)", re.I | re.S)
NEGATIVE = re.compile(r"(?:do not|don't|no need to|not asking you to).{0,20}(?:review|approve)|\bFYI only\b", re.I)
PR_URL = re.compile(r"https://(?:github\.com/[^/\s<>\"')]+/[^/\s<>\"')]+/pull/\d+|dev\.azure\.com/[^/\s<>\"')]+/[^/\s<>\"')]+/_git/[^/\s<>\"')]+/pullrequest/\d+|[^./\s<>\"']+\.visualstudio\.com/[^/\s<>\"')]+/_git/[^/\s<>\"')]+/pullrequest/\d+)", re.I)
TEAMS_URL = re.compile(r"https://teams\.microsoft\.com/l/message/[^\s<>\"\])]+", re.I)
PR_CHANNEL = re.compile(r"\b(?:pr|code)\b.*\breviews?\b|^pull requests?$", re.I)


def instant(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    # Graph/.NET trims fractional seconds to 1/2 digits and can emit 7 digits;
    # launchd's Python 3.9 accepts only 3 or 6. Normalize, without guessing a zone.
    value = re.sub(r"\.(\d+)(?=Z|[+-]\d{2}:\d{2}$|$)", lambda m: "." + (m.group(1) + "000000")[:6], value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ReadError("Teams timestamp has no timezone")
    return parsed


def config_path():
    return w.ROOT / "teams-config.json"


def state_path():
    return w.ROOT / "teams-state.json"


class ReadError(RuntimeError):
    pass


class WorkIQAuthHeld(ReadError):
    pass


class WorkIQDeferred(ReadError):
    pass


def workiq(args, timeout=70, many=False):
    proc = subprocess.Popen([str(WORKIQ), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    with _ACTIVE_LOCK:
        _ACTIVE_READS.append(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise ReadError("WorkIQ read timed out")
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_READS.remove(proc)
    if proc.returncode in (76,79) and 'TEAMS_READER_' in err:
        raise WorkIQDeferred('WorkIQ shared broker/service backoff; not a request for repeated sign-in')
    if proc.returncode==77 and 'TEAMS_READER_' in err:
        raise WorkIQAuthHeld('This WorkIQ source needs additional sign-in/access; other source scans continue without interactive UI')
    if proc.returncode in (75,78) and ('TEAMS_READER_' in err):
        raise WorkIQAuthHeld('WorkIQ silent authentication held; see agent-workflow teams-auth-status. No interactive sign-in attempted')
    try:
        if many:
            data=[];remaining=out.lstrip();decoder=json.JSONDecoder()
            while remaining:
                value,end=decoder.raw_decode(remaining);data.append(value);remaining=remaining[end:].lstrip()
        else:
            data = json.loads(out)
    except ValueError:
        # WorkIQ can return exit=0, empty stdout, and a structured 4xx on stderr.
        try:
            failure = json.loads(err)
            status = (failure.get('statuses') or [None])[0] or (failure.get("results") or [{}])[0].get("statusCode")
        except (ValueError, AttributeError):
            status = None
        raise ReadError("WorkIQ read unavailable" + (" (HTTP " + str(status) + ")" if status else ""))
    values=data if many else [data]
    if proc.returncode or not values or any(not isinstance(value,dict) or value.get('error') or value.get('isError') for value in values):
        raise ReadError("WorkIQ returned an error, not an empty successful result")
    return data


def graph_path(value):
    if value.startswith(('http:', 'https:')):
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != 'https' or parsed.hostname != 'graph.microsoft.com' or parsed.port not in (None, 443):
            raise ReadError('Refusing a non-Graph continuation host')
        if not parsed.path.startswith(('/v1.0/', '/beta/')):
            raise ReadError('Unsupported Graph continuation version')
        path = parsed.path.split('/', 2)[2]
        value = '/' + path + ('?' + parsed.query if parsed.query else '')
    # Graph returns valid reply cursors in both slash and OData-key syntax.
    value = re.sub(r"/(teams|channels|messages|chats)\('([^']+)'\)",
        lambda m: '/' + m.group(1) + '/' + urllib.parse.quote(urllib.parse.unquote(m.group(2)), safe=''), value)
    me_path = value == "/me" or value.startswith(("/me?", "/me/"))
    if not (me_path or value.startswith(("/chats/", "/teams/"))) or value.startswith("//"):
        raise ReadError("Refusing a non-Graph/unscoped continuation")
    return value


def fetch(config, path):
    return workiq(["fetch", "--account", config["account"], "--urls", graph_path(path)])


def fetch_many(config, paths):
    paths=[graph_path(path) for path in paths]
    values=workiq(['fetch','--account',config['account'],'--urls',*paths],many=True)
    if len(values)!=len(paths):raise ReadError('WorkIQ batch response incomplete; no source watermark advanced')
    return values


def collection(config, path, limit=30):
    values, seen = [], set()
    for _ in range(limit):
        path = graph_path(path)
        if path in seen:
            raise ReadError("Teams pagination did not advance")
        seen.add(path)
        data = fetch(config, path)
        if not isinstance(data.get("value"), list):
            raise ReadError("Missing Teams collection; not proof of no requests")
        values.extend(data["value"])
        path = data.get("@odata.nextLink")
        if not path:
            return values
    raise ReadError("Teams collection incomplete at bounded pagination limit")


class MessageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.links, self.ignored = [], [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ("blockquote", "script", "style", "pre", "code"):
            self.ignored += 1
        if self.ignored:
            return
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(unescape(attrs["href"]))
        if tag in ("p", "div", "br", "li"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("blockquote", "script", "style", "pre", "code"):
            self.ignored = max(0, self.ignored - 1)
    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def unwrap_url(value):
    for _ in range(2):
        u = urllib.parse.urlsplit(value)
        if not (u.hostname or "").endswith(".safelinks.protection.outlook.com"):
            break
        value = urllib.parse.parse_qs(u.query).get("url", [value])[0]
    return value


def eligible_url(config, value):
    try:
        value = w.canonical_url(value)
    except ValueError:
        return None
    u = urllib.parse.urlsplit(value)
    if u.hostname == "dev.azure.com":
        parts = u.path.split("/")
        allowed = config.get('ado_organizations', [])
        projects = config.get('ado_projects', {})
        return value if parts[1].lower() in [org.lower() for org in allowed] and (
            not projects.get(parts[1]) or urllib.parse.unquote(parts[2]) in projects[parts[1]]) else None
    owner = u.path.split("/")[1].lower()
    return value if owner in [x.lower() for x in config["github_owners"]] else None


def linked_prs(config, message):
    parser = MessageText()
    parser.feed((message.get("body") or {}).get("content") or "")
    values = parser.links + [" ".join("".join(parser.parts).split())]
    urls = set()
    for value in values:
        for match in PR_URL.finditer(unwrap_url(value)):
            url = eligible_url(config, match.group())
            if url:
                urls.add(url)
    return sorted(urls)


def parent_context(config, message, source):
    parent = message.get("replyToId")
    if not parent or parent == message.get("id") or not source.get("message_base"):
        return []
    path = source["message_base"] + "/" + urllib.parse.quote(str(parent), safe="")
    root = fetch(config, path)
    urls = linked_prs(config, root)
    # A reply to a single-PR thread is resolvable; multiple possible PRs are not.
    return [{"graph_path": path, "url": urls[0]}] if len(urls) == 1 else []


def candidate(config, message, source):
    sender = ((message.get("from") or {}).get("user") or {})
    if not sender.get("id") or sender["id"].lower() == config["self_id"].lower():
        return None
    if message.get("deletedDateTime") or message.get("messageType", "message") != "message":
        return None
    # Reactions modify lastModifiedDateTime on old messages; they are not new
    # delegation. Only a new message or actual content edit crosses the mandate.
    created_or_edited = max(instant(message.get("createdDateTime")), instant(message.get("lastEditedDateTime")))
    if created_or_edited < instant(config["since"]):
        return None
    body = message.get("body") or {}
    parser = MessageText()
    parser.feed(body.get("content") or "")
    text = " ".join("".join(parser.parts).split())
    if not REVIEW.search(text) or NEGATIVE.search(text):
        return None
    mentioned = {str(((m.get("mentioned") or {}).get("user") or {}).get("id", "")).lower()
                 for m in message.get("mentions", [])}
    names = config.get('display_names', []) + [config['account']]
    named = any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I) for name in names if name)
    directed = config["self_id"].lower() in mentioned or bool(named) or source.get("chat_type") == "oneOnOne"
    if not directed:
        return None  # Generic team-wide PR links are not assignments to this user.
    return {"urls": linked_prs(config, message), "text": text[:2000], "sender_id": sender["id"],
            "sender_name": sender.get("displayName"), "message_id": message.get("id"),
            "created_or_edited_at": created_or_edited.isoformat(), "source": source}


def update_request(key, patch):
    w.job_path(key)  # Reject path traversal even for programmatic callers.
    with w.lock("inbox-" + key):
        path = w.ROOT / "inbox" / (key + ".json")
        current = w.load(path) if path.exists() else {}
        current.update(patch)
        w.save(path, current)
        return current


def assert_identity(config):
    me = fetch(config, "/me?$select=id,userPrincipalName")
    if me.get("id", "").lower() != config["self_id"].lower() or me.get("userPrincipalName", "").lower() != config["account"].lower():
        raise ReadError("WorkIQ identity differs from the authorized account; intake held")


def conversation_followups(config, anchor, source, messages, message_base):
    """Bind only a short same-author continuation of a directed review request."""
    intent = candidate(config, anchor, source)
    if not intent or intent["urls"]:
        return []
    start = instant(intent["created_or_edited_at"])
    ordered = sorted(messages, key=lambda m: max(instant(m.get("createdDateTime")), instant(m.get("lastEditedDateTime"))))
    context, examined = [], 0
    for message in ordered:
        if message.get("id") == anchor.get("id"):
            continue
        stamp = max(instant(message.get("createdDateTime")), instant(message.get("lastEditedDateTime")))
        if stamp < start or stamp > start + timedelta(minutes=5):
            continue
        if message.get("deletedDateTime") or message.get("messageType", "message") != "message":
            continue
        if source["kind"] == "chat" and message.get("chatId") not in (None, source.get("chat_id")):
            continue
        examined += 1
        if examined > 20:
            break
        sender = ((message.get("from") or {}).get("user") or {}).get("id", "")
        parser = MessageText(); parser.feed((message.get("body") or {}).get("content") or "")
        text = " ".join("".join(parser.parts).split())
        if sender.lower() == config["self_id"].lower() and NEGATIVE.search(text):
            return []
        if sender.lower() != intent["sender_id"].lower():
            continue
        # A new request, cancellation, or different recipient ends this context.
        mentioned = {str(((m.get("mentioned") or {}).get("user") or {}).get("id", "")).lower()
                     for m in message.get("mentions", [])}
        if NEGATIVE.search(text):
            return []
        if REVIEW.search(text) or (mentioned and config["self_id"].lower() not in mentioned):
            break
        if source["kind"] == "chat" and message.get("replyToId") not in (None, anchor.get("id"), anchor.get("replyToId")):
            continue
        for url in linked_prs(config, message):
            context.append({"kind": "conversation_followup", "url": url,
                "graph_path": message_base + "/" + urllib.parse.quote(str(message["id"]), safe=""),
                "message_id": message["id"], "anchor_id": anchor["id"], "created_or_edited_at": stamp.isoformat()})
    return context


def fetch_followups(config, anchor, source):
    intent = candidate(config, anchor, source)
    if not intent or intent["urls"]:
        return []
    if source["kind"] == "chat":
        base = source["message_base"]
        lower = instant(intent["created_or_edited_at"]) - timedelta(seconds=1)
        # Do not impose an upper lastModified bound: a reaction can modify a
        # correctly adjacent older link. The pure matcher uses creation/edit time.
        query = urllib.parse.urlencode({"$top": "50", "$orderby": "lastModifiedDateTime desc",
            "$filter": "lastModifiedDateTime gt " + lower.isoformat().replace("+00:00", "Z")})
        path = base + "?" + query
    else:
        root = source["graph_path"].split("/replies/", 1)[0]
        base = root + "/replies"
        path = base + "?$top=50"
    messages = collection(config, path, limit=3)
    return conversation_followups(config, anchor, source, messages, base)


def resolve_missing_context(config, deadline):
    attempted = 0
    for request in inbox():
        if time.monotonic() >= deadline or attempted >= 3:
            break
        if request["status"] != "needs_context" or request.get("context_retry_epoch", 0) > time.time():
            continue
        attempted += 1
        resolved = set()
        context_error = None
        for evidence in request["evidence"].values():
            try:
                source = evidence["source"]
                anchor = fetch(config, source["graph_path"])
                context = parent_context(config, anchor, source) or fetch_followups(config, anchor, source)
                if context:
                    record(config, anchor, source, context_sources=context)
                    resolved.update(c["url"] for c in context)
            except ReadError as error:
                context_error = str(error)
        patch = {"context_checked_at": w.utc(), "context_retry_epoch": time.time() + 300, "context_error": context_error}
        if resolved:
            patch.update(status="context_linked", resolved_urls=sorted(resolved),
                resolved_requests=["teams-" + w.digest(url)[:20] for url in sorted(resolved)], context_error=None)
        update_request(request["id"], patch)


def verify_request(config, request):
    assert_identity(config)
    had_error = False
    for evidence in request["evidence"].values():
        try:
            message = fetch(config, evidence["source"]["graph_path"])
            if not message.get("id"):
                raise ReadError("Incomplete individual source message")
            current = candidate(config, message, evidence["source"])
            if current and request["url"] in current["urls"]:
                return True
            if current and not current["urls"]:
                context = parent_context(config, message, evidence["source"])
                if any(c["url"] == request["url"] for c in context):
                    return True
                recorded = [c for c in evidence.get("context_sources", []) if c.get("kind") == "conversation_followup" and c["url"] == request["url"]]
                if recorded and not context:
                    fresh = fetch_followups(config, message, evidence["source"])
                    if any(c["url"] == request["url"] and any(c["graph_path"] == old["graph_path"] for old in recorded) for c in fresh):
                        return True
        except ReadError:
            had_error = True
    if had_error:
        raise ReadError("Could not re-verify request; no review started")
    return False


def record(config, message, source, context_sources=None, resolve_parent=True):
    request = candidate(config, message, source)
    if not request:
        return 0
    if not request["urls"] and context_sources:
        request["context_sources"] = context_sources
        request["urls"] = sorted({c["url"] for c in context_sources})
    if not request["urls"] and resolve_parent:
        try:
            context = parent_context(config, message, source)
            if context:
                request["context_sources"] = context
                request["urls"] = [c["url"] for c in context]
        except ReadError:
            request["context_error"] = "Parent thread unavailable; no PR inferred"
    # Do not silently drop directed requests lacking a full PR URL. Retain them
    # for bounded context triage, but never invent a PR or auto-vote on inference.
    urls = request["urls"] or [None]
    for url in urls:
        key = "teams-" + w.digest(url or [source.get("graph_path"), request["message_id"]])[:20]
        evidence_id = w.digest([source.get("graph_path"), request["message_id"], url])
        with w.lock("inbox-" + key):
            path = w.ROOT / "inbox" / (key + ".json")
            old = w.load(path) if path.exists() else {}
            evidence = old.get("evidence", {})
            evidence[evidence_id] = request
            entry = {**old, "id": key, "url": url, "evidence": evidence,
                     "account": config["account"], "self_id": config["self_id"], "updated_at": w.utc()}
            if not old:
                entry.update(status="pending" if url else "needs_context", created_at=w.utc(), attempts=0)
            for j in w.jobs():
                if url and j["url"] == url:
                    entry.update(status="linked", job=j["id"])
            if url and w.legacy_watcher(url):
                entry.update(status="existing_watch", decision="Existing narrower watcher retained; no duplicate review worker")
            w.save(path, entry)
        source_path = w.ROOT / "teams-sources" / (evidence_id + ".json")
        w.save(source_path, {"message": message, "source": source})
    return len(urls)


def priority_channel(config, channel):
    return (channel.get('channel_id') in config.get('direct_channel_ids', [])
            or channel.get('name', '').casefold() in {v.casefold() for v in config.get('direct_channel_names', [])}
            or bool(PR_CHANNEL.search(channel.get('name', ''))))


def discover(config, state):
    if time.time() - state.get("discovery_epoch", 0) < 3600:
        state["priority_channels"] = [c for c in state.get("channels", {}).values() if priority_channel(config, c)]
        return
    teams = collection(config, "/me/joinedTeams?$select=id,displayName")
    def channels(team):
        try:
            values = collection(config, "/teams/" + urllib.parse.quote(team["id"], safe="") + "/channels?$select=id,displayName")
            return team, values, None
        except ReadError as error:
            return team, [], str(error)
    mapping, priority, gaps = {}, [], {}
    with ThreadPoolExecutor(max_workers=2 if config.get('background_silent_only') else 3) as pool:
        for team, values, error in pool.map(channels, teams):
            if error:
                gaps[team["id"]] = error
                # A deferred team read is not proof its known channels vanished.
                for existing in state.get('channels',{}).values():
                    if existing.get('team_id')==team['id']:
                        mapping[existing['channel_id']]=existing
                        if priority_channel(config,existing):priority.append(existing)
            for channel in values:
                item = {"kind": "channel", "team_id": team["id"], "channel_id": channel["id"],
                        "name": channel.get("displayName"), "team_name": team.get("displayName")}
                mapping[channel["id"]] = item
                if priority_channel(config, item):
                    priority.append(item)
    state.update(channels=mapping, priority_channels=priority, discovery_gaps=gaps,
                 joined_team_ids=[t["id"] for t in teams], discovery_epoch=time.time())


def read_messages(config, state, source, start_path, deadline):
    key = source.get("chat_id") or source["channel_id"]
    progress = state.setdefault("message_cursors", {}).get(key, {})
    path = progress.get("next") or start_path
    pages = 0
    while path and pages < 10 and time.monotonic() < deadline:
        data = fetch(config, path)
        if not isinstance(data.get("value"), list):
            raise ReadError("Missing message collection")
        for message in data["value"]:
            ident = urllib.parse.quote(str(message.get("id", "")), safe="")
            src = {**source, "graph_path": source["message_base"] + "/" + ident}
            src["web_url"] = message.get("webUrl")
            record(config, message, src)
            for reply in message.get("replies", []):
                child = {**src, "graph_path": src["graph_path"] + "/replies/" + urllib.parse.quote(str(reply.get("id", "")), safe="")}
                child["web_url"] = reply.get("webUrl")
                record(config, reply, child)
            if message.get("replies@odata.nextLink"):
                state.setdefault("coverage_gaps", {})[key] = "Expanded reply pagination incomplete; search reconciliation is still needed"
        path = data.get("@odata.nextLink")
        pages += 1
        # Dedicated-channel reads are deliberately bounded to the latest page;
        # WorkIQ's indexed reconciler also checks requests/replies in old threads.
        if source["kind"] == "channel":
            path = None
    if path:
        state["message_cursors"][key] = {"next": graph_path(path), "source": source, "start": start_path}
    else:
        state["message_cursors"].pop(key, None)


def poll_chats(config, state, deadline):
    previous_scan = instant(state["last_chat_scan"]) if state.get("last_chat_scan") else instant(config["since"])
    cutoff = max(instant(config["since"]), previous_scan - timedelta(minutes=10))
    started = w.utc()
    had_pending = bool(state.get("message_cursors"))
    path = "/me/chats?$top=50&$expand=lastMessagePreview&$orderby=lastMessagePreview/createdDateTime desc"
    complete = False
    for _ in range(20):
        if time.monotonic() >= deadline:
            break
        data = fetch(config, path)
        if not isinstance(data.get("value"), list):
            raise ReadError("Missing chat metadata collection")
        recent = [c for c in data["value"] if instant((c.get("lastMessagePreview") or {}).get("createdDateTime")) >= cutoff]
        for chat in recent:
            base = "/chats/" + urllib.parse.quote(chat["id"], safe="") + "/messages"
            source = {"kind": "chat", "chat_id": chat["id"], "chat_type": chat.get("chatType"),
                      "name": chat.get("topic"), "message_base": base}
            query = urllib.parse.urlencode({"$top": "50", "$orderby": "lastModifiedDateTime desc",
                "$filter": "lastModifiedDateTime gt " + cutoff.isoformat().replace("+00:00", "Z")})
            read_messages(config, state, source, base + "?" + query, deadline)
        path = data.get("@odata.nextLink")
        # The API's explicitly requested preview-date ordering makes this a
        # recency boundary, not a relevance-ranked search completeness claim.
        if not path or len(recent) < len(data["value"]):
            complete = True
            break
    if complete and not had_pending and not state.get("message_cursors"):
        state["last_chat_scan"] = started
        state.get('coverage_gaps', {}).pop('chat_discovery', None)
    else:
        state.setdefault("coverage_gaps", {})["chat_discovery"] = "Chat discovery budget/page bound reached; watermark retained"
    for cursor in list(state.get("message_cursors", {}).values()):
        if time.monotonic() >= deadline:
            break
        read_messages(config, state, cursor["source"], cursor["start"], deadline)


def source_from_link(config, state, value):
    u = urllib.parse.urlsplit(unescape(value.rstrip(".,)")))
    if u.scheme != "https" or u.hostname != "teams.microsoft.com" or not u.path.startswith("/l/message/"):
        raise ReadError("Not an actual Teams message link")
    parts = u.path.split("/")
    if len(parts) < 5:
        raise ReadError("Incomplete Teams message link")
    conversation, message_id = urllib.parse.unquote(parts[3]), urllib.parse.unquote(parts[4])
    if not re.fullmatch(r"\d+", message_id) or not conversation.startswith("19:"):
        raise ReadError("Not a supported individual Teams message link")
    query = urllib.parse.parse_qs(u.query)
    channel = state.get("channels", {}).get(conversation)
    if not channel and query.get("groupId", [None])[0] in state.get("joined_team_ids", []):
        channel = {"kind": "channel", "team_id": query["groupId"][0], "channel_id": conversation}
    if channel:
        base = "/teams/" + urllib.parse.quote(channel["team_id"], safe="") + "/channels/" + urllib.parse.quote(conversation, safe="") + "/messages"
        parent = query.get("parentMessageId", [message_id])[0]
        path = base + "/" + urllib.parse.quote(parent, safe="")
        if parent != message_id:
            path += "/replies/" + message_id
        return {**channel, "message_base": base, "graph_path": path, "web_url": value}
    chat = fetch(config, "/me/chats/" + urllib.parse.quote(conversation, safe=""))
    base = "/chats/" + urllib.parse.quote(conversation, safe="") + "/messages"
    return {"kind": "chat", "chat_id": conversation, "chat_type": chat.get("chatType"),
            "name": chat.get("topic"), "message_base": base, "graph_path": base + "/" + message_id, "web_url": value}


def reconcile_search(config, state, timeout=150):
    state.update(search_status='disabled: silent-only backend uses direct reads, never interactive search', search_error=None)


def poll_all_sources(config):
    with w.lock('teams-intake', nonblocking=True) as acquired:
        if not acquired:return
        state=w.load(state_path()) if state_path().exists() else {}
        try:
            assert_identity(config)
            import teams_coverage
            teams_coverage.run(config,state,time.monotonic()+210)
            import teams_reader
            teams_reader.reconcile()
            state['last_poll_at']=w.utc()
            if teams_reader.backoff_active():
                state['error']='Teams reads deferred by shared broker/service cooldown; see teams-auth-status'
            else:
                state['last_success_at']=w.utc();state.pop('error',None)
        except Exception as error:
            text=str(error) if isinstance(error,ReadError) else 'Teams all-source scan unavailable ('+type(error).__name__+')'
            if state.get('error')!=text:w.notify(text)
            state.update(error=text,last_poll_at=w.utc())
        w.save(state_path(),state)


def poll():
    if not config_path().exists():
        return
    config = w.load(config_path())
    if not config.get("enabled"):
        return
    import configuration
    configuration.intake('teams', config, require_enabled=True)
    if config.get('scan_all_sources'):
        return poll_all_sources(config)
    with w.lock("teams-intake", nonblocking=True) as acquired:
        if not acquired:
            return
        state = w.load(state_path()) if state_path().exists() else {}
        deadline = time.monotonic() + 210
        try:
            assert_identity(config)
            discover(config, state)
            # Explicit channel coverage must not sit behind an expensive chat scan.
            channels = sorted(state.get('priority_channels', []), key=lambda c: c['channel_id'] not in config.get('direct_channel_ids', []))
            for source in channels[:12]:
                if time.monotonic() >= deadline:
                    break
                base = "/teams/" + urllib.parse.quote(source["team_id"], safe="") + "/channels/" + urllib.parse.quote(source["channel_id"], safe="") + "/messages"
                scan = {**state.get('channel_scans', {}).get(source['channel_id'], {}), 'last_attempt_at': w.utc(), 'name': source.get('name')}
                try:
                    previous_gap = state.get('coverage_gaps', {}).get(source['channel_id'])
                    read_messages(config, state, {**source, "message_base": base, 'discovery': 'direct_channel'}, base + "?$top=50&$expand=replies", deadline)
                    scan.update(last_success_at=w.utc(), error=None)
                    if previous_gap and state.get('coverage_gaps', {}).get(source['channel_id']) == previous_gap and 'reply pagination' not in previous_gap:
                        state['coverage_gaps'].pop(source['channel_id'], None)
                except ReadError as error:
                    scan['error'] = str(error)
                    state.setdefault("coverage_gaps", {})[source["channel_id"]] = str(error)
                state.setdefault('channel_scans', {})[source['channel_id']] = scan
            if time.monotonic() < deadline:
                resolve_missing_context(config, deadline)
                poll_chats(config, state, min(deadline, time.monotonic()+90))
            if time.monotonic() < deadline:
                resolve_missing_context(config, deadline)
            # Search is explicitly best-effort/indexed; it complements direct
            # reads, not a claim of exhaustive, immediate channel ingestion.
            if time.monotonic() < deadline:
                try:
                    reconcile_search(config, state)
                    state.pop("search_error", None)
                except ReadError as error:
                    state["search_error"] = str(error)
            state.update(last_poll_at=w.utc(), last_success_at=w.utc())
            state.pop("error", None)
        except Exception as error:
            text = str(error) if isinstance(error, ReadError) else "Teams intake unavailable (" + type(error).__name__ + ")"
            if state.get("error") != text:
                w.notify(text)
            state.update(error=text, last_poll_at=w.utc())
        w.save(state_path(), state)


def inbox():
    return sorted([w.load(p) for p in (w.ROOT / "inbox").glob("teams-*.json")], key=lambda r: r.get("created_at", ""))


def coverage_summary():
    state = w.load(state_path()) if state_path().exists() else {}
    result = {k: state.get(k) for k in ("last_poll_at", "last_success_at", "last_chat_scan", "last_search_at", "error", "search_error", "search_status", "search_references", "unresolved_search_references", "discovery_gaps", "coverage_gaps")}
    if config_path().exists() and w.load(config_path()).get('background_silent_only'):
        import teams_reader
        result['silent_authentication']=teams_reader.status()
    if config_path().exists() and w.load(config_path()).get('scan_all_sources'):
        import teams_coverage
        result['direct_scan_progress']=teams_coverage.progress_summary(state)
    result.update(joined_teams=len(state.get("joined_team_ids", [])), mapped_channels=len(state.get("channels", {})),
                  direct_pr_channels=[c.get("name") for c in state.get("priority_channels", [])],
                  all_source_coverage=state.get('all_source_coverage'),
                  chat_catalogue_error=state.get('chat_catalogue_error'),
                  direct_channel_scans=state.get('channel_scans', {}),
                  pending_message_cursors=len(state.get("message_cursors", {})),
                  channel_coverage=('All accessible discovered chats/channels are scheduled for direct cursor-based reads; inspect all_source_coverage for unfinished/failed sources' if w.load(config_path()).get('scan_all_sources') else 'Dedicated PR channels read directly; other channels/replies use best-effort indexed reconciliation, not exhaustive realtime ingestion') if config_path().exists() else 'Not configured')
    return result


def prepare_one(request_id=None):
    """Called by the main PR scheduler under its global worker lock."""
    if not config_path().exists():
        return False
    config = w.load(config_path())
    if not config.get("enabled") or not config.get("auto_start_reviews"):
        return False
    if len(w.recent_cycles()) >= w.global_cycle_limit():
        return False
    for request in inbox():
        if request_id is not None and request["id"] != request_id:
            continue
        if request["status"] == "preparing":
            pid = request.get("worker_pid")
            if pid and w.process_identity(pid) == request.get("worker_identity"):
                continue
            request = update_request(request["id"], {"status": "pending", "updated_at": w.utc()})
        if request["status"] != "pending" or request.get("next_retry_epoch", 0) > time.time():
            continue
        for job in w.jobs():
            if job["url"] == request["url"]:
                request = update_request(request["id"], {"status": "linked", "job": job["id"]})
                break
        if request["status"] != "pending":
            continue
        # Re-fetch actual evidence and terminal/self-author metadata, never act
        # just because search prose says someone requested a review.
        try:
            if not verify_request(config, request):
                update_request(request["id"], {"status": "withdrawn_or_unverifiable", "updated_at": w.utc()})
                continue
            metadata = w.snapshot({'url': request['url'], **({'github_login': request['github_login']} if request.get('github_login') else {})})
            if metadata["state"] in w.TERMINAL:
                update_request(request["id"], {"status": "terminal", "terminal_state": metadata["state"], "updated_at": w.utc()})
                continue
            author = metadata.get("author")
            if isinstance(author, dict) and (author.get("uniqueName") or "").lower() == config["account"].lower():
                update_request(request["id"], {"status": "self_authored", "decision": "No self-review approval; retain author workflow", "updated_at": w.utc()})
                continue
            if isinstance(author, str):
                import probes
                if request.get('github_login'):
                    import github_auth
                    actor = github_auth.run_json(['api','user'], request['github_login'])
                else:
                    import configuration
                    login = config.get('github_login') or configuration.controller().get('github_login')
                    if not login:
                        raise ReadError('Teams GitHub review requires an explicit pinned login')
                    import github_auth
                    actor = github_auth.run_json(['api', 'user'], login)
                    github_auth.verified(login, actor)
                w.save(w.ROOT / "teams-github-identity.json", {"login": actor["login"]})
                request = update_request(request['id'], {'github_login': actor['login'], 'github_user_id': actor.get('id')})
                github_config = w.ROOT/'github-config.json'
                own_logins = w.load(github_config).get('logins', []) if github_config.exists() else [actor['login']]
                if author.lower() in [login.lower() for login in own_logins]:
                    update_request(request["id"], {"status": "self_authored", "decision": "No self-review approval", "updated_at": w.utc()})
                    continue
        except Exception as error:
            import auth_recovery
            import github_auth
            if isinstance(error, WorkIQDeferred):
                import teams_reader
                update_request(request['id'], {'last_error':'Shared WorkIQ cooldown; source verification will retry without sign-in UI',
                    'next_retry_epoch':teams_reader.load_state().get('retry_after') or time.time()+300})
            elif isinstance(error, WorkIQAuthHeld):
                update_request(request['id'], {'status': 'teams_auth_wait', 'teams_auth_hold': True,
                    'last_error': 'Silent WorkIQ source verification held; see teams-auth-status', 'next_retry_epoch':time.time()+300})
            elif isinstance(error, github_auth.GitHubAuthError):
                update_request(request['id'], {'status': 'auth_wait', 'github_auth_hold': True,
                    'last_error': 'GitHub sign-in needs help; the watcher will verify recovery'})
            elif isinstance(error, auth_recovery.AuthError):
                auth_recovery.failure('deferred', {'kind': 'intake', 'id': request['id'], 'url': request['url']})
                update_request(request['id'], {'status': 'auth_wait', 'auth_hold': True,
                    'last_error': 'Authentication held; see agent-workflow auth-status', 'next_retry_epoch': time.time()+60})
            else:
                update_request(request["id"], {"last_error": "Source/PR verification unavailable (" + type(error).__name__ + "); no review started", "next_retry_epoch": time.time() + 1800})
            continue
        return prepare_request(request)
    return False


def prepare_request(request):
    path = w.ROOT / "inbox" / (request["id"] + ".json")
    if request.get("attempts", 0) - request.get("auth_retry_count", 0) - request.get('transport_retry_count', 0) >= 2:
        update_request(request["id"], {"status": "blocked", "decision": "Review worktree preparation failed twice; inspect its logs"})
        return False
    workstream = "autoworkflow-" + request["id"]
    routing = {k: request[k] for k in ("id", "url", "account", "self_id")}
    origin = "Teams"
    if request.get("source_kind") in ('ado', 'github'):
        origin = "ADO reviewer assignment" if request['source_kind']=='ado' else 'GitHub reviewer assignment'
        routing["assignment"] = request["assignment"]
    else:
        routing["sources"] = [{"graph_path": e["source"]["graph_path"], "web_url": e["source"].get("web_url"),
                                "sender_id": e["sender_id"], "message_id": e["message_id"],
                                "context_sources": e.get("context_sources", [])} for e in request["evidence"].values()]
    import configuration, prompts
    configuration.require_background()
    prompt = prompts.preparation(request, routing, workstream)
    command = w.worker_command(request)
    import auth_recovery
    env = auth_recovery.worker_env(os.environ.copy())
    import github_auth
    import teams_reader
    env = github_auth.worker_env(env, request.get('github_login'))
    for key in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_TERM_NAME", "CLAUDE_SESSION_ID", "CLAUDE_TERM_NAME", "AUTOWORKFLOW_JOB", "AUTOWORKFLOW_CYCLE"):
        env.pop(key, None)
    env.update(PI_TERM_NAME=workstream, AUTOWORKFLOW_INTAKE=request["id"], AUTOWORKFLOW_INTAKE_URL=request["url"])
    log_path = w.ROOT / "logs" / (request["id"] + "-prepare-" + str(int(time.time())) + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not w.reserve_cycle():
        return False
    request = update_request(request["id"], {"status": "preparing", "attempts": request.get("attempts", 0) + 1, "updated_at": w.utc()})
    with log_path.open("w") as log:
        proc = subprocess.Popen(command, cwd=str(Path.home()), env=env, stdin=subprocess.PIPE, stdout=log,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        request = update_request(request["id"], {"worker_pid": proc.pid, "worker_identity": w.process_identity(proc.pid)})
        w.action_notices.safe(w.action_notices.preparation_started, request)
        def stop():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        previous = {}
        def cancelled(_sig, _frame):
            stop()
            raise KeyboardInterrupt("Review preparation stopped")
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, cancelled)
            proc.communicate(prompt, timeout=1200)
        except subprocess.TimeoutExpired:
            stop()
        finally:
            stop()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            linked = next((j for j in w.jobs() if j.get("intake") == request["id"] and j["mode"] == "review" and j["url"] == request["url"]), None)
            patch = {"last_log": str(log_path), "last_exit": proc.returncode, "updated_at": w.utc(), "worker_pid": None, "worker_identity": None}
            import worker_recovery
            transport = worker_recovery.preparation_transport_failure(request, log_path, proc.returncode) if not linked else None
            if proc.returncode == 0 and linked and linked["status"] in ("ready", "waiting"):
                patch.update(status="linked", job=linked["id"])
            elif auth_recovery.held('intake', request['id']) or github_auth.held('intake', request['id']):
                patch.update(status='auth_wait', auth_hold=True,
                    auth_retry_count=request.get('auth_retry_count', 0)+1,
                    decision=None, last_error='Preparation interrupted by authentication; automatic recovery/human escalation is active')
                if linked:
                    with w.lock('registry'):
                        latest=w.load(w.job_path(linked['id']))
                    if github_auth.held('intake', request['id']):github_auth.hold_job(latest)
                    else:auth_recovery.hold_job(latest)
                    w.save(w.job_path(linked['id']),latest)
                    patch['job']=linked['id']
            elif teams_reader.held('intake',request['id']):
                patch.update(status='teams_auth_wait',teams_auth_hold=True,decision=None,
                    auth_retry_count=request.get('auth_retry_count',0)+1,next_retry_epoch=time.time()+300,
                    last_error='Preparation source verification needs silent WorkIQ recovery; see teams-auth-status')
                if linked:
                    with w.lock('registry'):
                        latest=w.load(w.job_path(linked['id']));teams_reader.hold_job(latest);w.save(w.job_path(linked['id']),latest)
                    patch['job']=linked['id']
            elif transport:
                patch.update(transport)
            else:
                patch.update(status="blocked", decision="Review preparation did not produce a verified handoff; inspect " + str(log_path))
                if linked:
                    with w.lock("registry"):
                        latest = w.load(w.job_path(linked["id"]))
                        latest.update(status="blocked", decision=patch["decision"], owner=None)
                        w.save(w.job_path(linked["id"]), latest)
                    patch["job"] = linked["id"]
                w.notify(patch["decision"])
            update_request(request["id"], patch)
    return True


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("poll", "inbox"))
    args = parser.parse_args()
    def cancel_reads(_signum, _frame):
        with _ACTIVE_LOCK:
            active = list(_ACTIVE_READS)
        for proc in active:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise KeyboardInterrupt("Teams intake stopped; no watermark advanced")
    signal.signal(signal.SIGTERM, cancel_reads)
    signal.signal(signal.SIGINT, cancel_reads)
    if args.command == "poll":
        poll()
    else:
        print(json.dumps([{k: r.get(k) for k in ("id", "url", "status", "job", "decision", "last_error", "updated_at")} for r in inbox()], indent=2))


if __name__ == "__main__":
    main()
