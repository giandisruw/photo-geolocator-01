#!/usr/bin/env python3
"""Photo geolocator agent for make.fun photo-geolocator-01.

Reads the 25 street-level photos of one instance from the env, places each
one (latitude/longitude) and posts the answers. Two paths, in order:

  1. Vision model through the make.fun gateway (Anthropic first, then
     OpenAI), one call per photo with a structured reasoning prompt.
     The gateway replaces the trial's stand-in key with the entrant's own
     API key; with no key pushed every call is refused (401) and the
     agent falls through to path 2.
  2. Code-only fallback: a single fixed coordinate (the median of the
     public practice pool, aggregate only -- no photo table is shipped).

Deterministic given the model's answers: photos are processed in fixed
order p01..p25, answers are posted in that order, no wall clock is read,
and any randomness is seeded from MAKEFUN_TRIAL_SEED. Model calls run
one at a time, in photo order, so the live run and its replay make the
same calls.

Standard library only. Run as: python3 -P -s /bot/main.py
Env (set by the trial):
  MAKEFUN_PROMPT_FILE  the brief (read, not required for the answers)
  MAKEFUN_ENV_URL      the env origin
  MAKEFUN_TRIAL_SEED   seed for any randomness
  ANTHROPIC_BASE_URL / OPENAI_BASE_URL   the model gateway
  ANTHROPIC_API_KEY / OPENAI_API_KEY     the trial's stand-in key
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------- config

# Model chains, best first. (model, max_tokens)
# Anthropic: opus is the most accurate documented vision model on the
# allowlist; sonnet is the proven budget-safe fallback; haiku is cheapest.
ANTHROPIC_MODELS = [
    ("claude-opus-5-5", 768),
    ("claude-sonnet-5-5", 1024),
    ("claude-haiku-5-5", 1024),
]
# OpenAI: 6.1-sol first, then 6-sol, then the cheapest (luna).
OPENAI_MODELS = [
    ("gpt-6.1-sol", 1024),
    ("gpt-6-sol", 1024),
    ("gpt-6-luna", 1024),
]

# How many times to retry a transient provider error (408/429/5xx/529).
RETRIES = 2
# Gateway refusals are final and are never retried.
GATEWAY_REFUSAL_TYPES = {
    "budget_exhausted", "request_cap", "model_not_allowed",
    "max_tokens_required", "request_too_large", "invalid_json",
    "stateful_request", "network_request",
}
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}

# Code-only fallback: the component-wise median of the 75 public practice
# photos (aggregate only; no photo or place table is shipped).
FALLBACK_LAT = 30.25
FALLBACK_LON = 21.03

# The reasoning prompt sent with every photo. Short reasoning first (it
# makes the guess much better than a bare coordinate), then one JSON line.
PROMPT = (
    "Look at this street-level photo. Work out where on Earth it was taken.\n"
    "Consider, in order: (1) the country - language and script on signs, "
    "driving side, licence plates, road markings, vegetation and climate, "
    "architecture, street furniture; (2) the region or city - any distinctive "
    "landmarks, vegetation type, building style, terrain; (3) the precise place.\n"
    "Give a short reasoning (2-4 sentences) naming the clues, then your best "
    "estimate as one line of JSON: {\"lat\": <degrees north>, \"lon\": <degrees east>}. "
    "If you are unsure, give your best guess anyway - never leave it blank."
)

# ---------------------------------------------------------------- helpers


def _log(msg):
    sys.stderr.write("geolocator: %s\n" % msg)
    sys.stderr.flush()


def _http(method, url, headers=None, body=None, timeout=120):
    """One HTTP exchange. Returns (status, bytes). Raises on no answer."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # The trial passes base URLs, never a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # connection failed before any answer
        raise IOError("no answer from %s: %s" % (url, e))


def _is_lat(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and -90.0 <= float(v) <= 90.0


def _is_lon(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and -180.0 <= float(v) <= 180.0


def coordinates(text):
    """Extract (lat, lon) from a model's answer.

    First JSON object with lat/lon keys, else the first two numbers in
    range. Returns (lat, lon) or None.
    """
    if not text:
        return None
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            o = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        lat = o.get("lat", o.get("latitude"))
        lon = o.get("lon", o.get("lng", o.get("longitude")))
        if _is_lat(lat) and _is_lon(lon):
            return (float(lat), float(lon))
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    for i in range(len(nums) - 1):
        if _is_lat(nums[i]) and _is_lon(nums[i + 1]):
            return (nums[i], nums[i + 1])
    return None


# ---------------------------------------------------------------- env


class Env:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")

    def list_photos(self):
        status, raw = _http("GET", self.base + "/photos",
                            {"Accept": "application/json"})
        if status != 200:
            raise IOError("GET /photos answered %d" % status)
        data = json.loads(raw.decode("utf-8"))
        photos = [p for p in data.get("photos", []) if not p.get("answered")]
        photos.sort(key=lambda p: p["id"])
        return photos

    def fetch_photo(self, photo):
        url = self.base + photo["url"]
        status, raw = _http("GET", url, {"Accept": "image/jpeg"})
        if status != 200:
            raise IOError("GET %s answered %d" % (photo["url"], status))
        return raw

    def post_answers(self, answers):
        """Post answers (list of {id, lat, lon}) in one batch.

        Returns (accepted_ids, answered, left). Raises on refusal.
        """
        body = json.dumps({"answers": answers}).encode("utf-8")
        status, raw = _http("POST", self.base + "/answers",
                            {"Content-Type": "application/json"}, body)
        if status != 200:
            raise IOError("POST /answers answered %d: %s"
                          % (status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- models


class ModelRefusal(Exception):
    """The gateway refused the call (final): budget, cap, allowlist, key."""

    def __init__(self, message, status, type_):
        super().__init__(message)
        self.status = status
        self.type = type_


class ModelError(Exception):
    """A transient model failure (retried, then given up on)."""


def _error_of(route, status, raw):
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    err = body.get("error") if isinstance(body, dict) else None
    info = err if isinstance(err, dict) else {}
    type_ = info.get("type") or info.get("code")
    message = info.get("message") or (body[:300] if isinstance(body, str) else "")
    what = "%s answered %d%s%s" % (route, status,
                                   (" " + str(type_)) if type_ else "",
                                   (": " + str(message)) if message else "")
    if type_ in GATEWAY_REFUSAL_TYPES or status == 401:
        return ModelRefusal(what, status, type_)
    return ModelError(what)


def _call_with_retry(route, method, url, headers, body):
    """POST a model call, retrying transient errors only.

    Returns the parsed JSON on success. Raises ModelRefusal (final) or
    ModelError (transient, retries exhausted).
    """
    attempt = 0
    while True:
        try:
            status, raw = _http(method, url, headers, body)
        except IOError as e:
            # No answer at all: the gateway never saw it, so it has no
            # recording to replay; retrying is safe.
            if attempt < RETRIES:
                attempt += 1
                continue
            raise ModelError("%s: %s" % (route, e))
        if 200 <= status < 300:
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ModelError("%s answered %d with a non-JSON body"
                                 % (route, status))
        err = _error_of(route, status, raw)
        if isinstance(err, ModelRefusal):
            raise err
        if status in RETRY_STATUSES and attempt < RETRIES:
            attempt += 1
            continue
        raise err


def ask_anthropic(base_url, api_key, model, max_tokens, jpeg):
    """One Anthropic Messages call. Returns the model's text, or raises."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode("ascii")}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }).encode("utf-8")
    data = _call_with_retry(
        "anthropic.messages", "POST", base_url.rstrip("/") + "/v1/messages",
        {"Accept": "application/json", "Content-Type": "application/json",
         "x-api-key": api_key, "anthropic-version": "2023-06-01"}, body)
    parts = []
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def ask_openai(base_url, api_key, model, max_tokens, jpeg):
    """One OpenAI Chat Completions call. Returns the model's text, or raises."""
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }).encode("utf-8")
    data = _call_with_retry(
        "openai.chat", "POST", base_url.rstrip("/") + "/chat/completions",
        {"Accept": "application/json", "Content-Type": "application/json",
         "Authorization": "Bearer " + api_key}, body)
    choices = data.get("choices", [])
    if not choices:
        return ""
    msg = choices[0].get("message", {})
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


# ---------------------------------------------------------------- agent


def _place_with_model(jpeg, vendor, models, base_url, api_key):
    """Try the vendor's model chain for one photo.

    Returns (lat, lon) from the first model that answers with usable
    coordinates, or None if every model answered but none gave
    coordinates. Raises ModelRefusal(401) when the vendor has no usable
    key at all, and ModelRefusal when no model answered at all (the
    vendor is broken or unreachable), so the caller can stop probing it.
    """
    answered_any = False
    no_key = True
    for model, max_tokens in models:
        try:
            if vendor == "anthropic":
                text = ask_anthropic(base_url, api_key, model, max_tokens, jpeg)
            else:
                text = ask_openai(base_url, api_key, model, max_tokens, jpeg)
        except ModelRefusal as e:
            if e.status == 401:
                # No key for this vendor: the next model would 401 too.
                continue
            no_key = False
            _log("model %s refused (%s); trying next" % (model, e))
            continue
        except ModelError as e:
            no_key = False
            _log("model %s errored (%s); trying next" % (model, e))
            continue
        answered_any = True
        no_key = False
        c = coordinates(text)
        if c is not None:
            return c
        _log("model %s gave no coordinates; trying next" % model)
    if no_key:
        raise ModelRefusal("vendor %s has no usable key" % vendor, 401, None)
    if not answered_any:
        raise ModelRefusal("vendor %s answered no model at all" % vendor,
                           0, None)
    return None


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").strip()
    if not env_url:
        _log("MAKEFUN_ENV_URL is not set")
        return 1
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read(1)  # the brief; answers come from the photos
        except OSError as e:
            _log("cannot read prompt file: %s" % e)

    env = Env(env_url)
    try:
        photos = env.list_photos()
    except IOError as e:
        _log("cannot list photos: %s" % e)
        return 1
    if not photos:
        _log("no unanswered photos")
        return 0
    _log("%d photos to place" % len(photos))

    # Fetch every photo first (env requests, in photo order).
    jpegs = {}
    for p in photos:
        try:
            jpegs[p["id"]] = env.fetch_photo(p)
        except IOError as e:
            _log("cannot fetch %s: %s" % (p["id"], e))
    if not jpegs:
        _log("no photo could be fetched")
        return 1

    # Probe the gateway once per vendor, on the first photo, to learn
    # which vendor (if any) will answer. A 401 means no key for it.
    anthropic_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    vendor = None  # ("anthropic", base_url, api_key) | ("openai", ...) | None
    first_id = photos[0]["id"]
    first_jpeg = jpegs.get(first_id)
    if first_jpeg is not None and anthropic_url and anthropic_key:
        try:
            c = _place_with_model(first_jpeg, "anthropic", ANTHROPIC_MODELS,
                                  anthropic_url, anthropic_key)
            vendor = ("anthropic", anthropic_url, anthropic_key)
            _log("gateway: anthropic available (probe %s -> %s)"
                 % (first_id, "coordinates" if c else "no coordinates"))
            # Remember the probe's answer for the first photo.
            probe_answer = {first_id: c}
        except ModelRefusal:
            _log("gateway: anthropic has no usable key")
            probe_answer = {}
            vendor = None
    else:
        probe_answer = {}
    if vendor is None and first_jpeg is not None and openai_url and openai_key:
        try:
            c = _place_with_model(first_jpeg, "openai", OPENAI_MODELS,
                                  openai_url, openai_key)
            vendor = ("openai", openai_url, openai_key)
            _log("gateway: openai available (probe %s -> %s)"
                 % (first_id, "coordinates" if c else "no coordinates"))
            probe_answer = {first_id: c}
        except ModelRefusal:
            _log("gateway: openai has no usable key")
            probe_answer = {}
            vendor = None
    if vendor is None:
        _log("gateway: no model available; placing with code only")

    # Place every photo, in order. The probe's answer (if any) is reused
    # for the first photo so the probe call is not repeated.
    answers = []
    for p in photos:
        pid = p["id"]
        jpeg = jpegs.get(pid)
        if jpeg is None:
            answers.append({"id": pid, "lat": FALLBACK_LAT, "lon": FALLBACK_LON})
            continue
        lat_lon = None
        if pid in probe_answer:
            lat_lon = probe_answer[pid]
        elif vendor is not None:
            kind, base_url, api_key = vendor
            models = ANTHROPIC_MODELS if kind == "anthropic" else OPENAI_MODELS
            try:
                lat_lon = _place_with_model(jpeg, kind, models, base_url, api_key)
            except ModelRefusal:
                # The vendor stopped answering mid-trial: fall through to
                # code-only for this and the remaining photos.
                vendor = None
                lat_lon = None
        if lat_lon is None:
            lat_lon = (FALLBACK_LAT, FALLBACK_LON)
        answers.append({"id": pid, "lat": round(lat_lon[0], 6),
                        "lon": round(lat_lon[1], 6)})

    # Post all answers. One batch; on refusal, retry in smaller batches
    # and then one by one, so a single bad answer cannot lose the rest.
    # (A photo already answered is final; a whole-batch refusal changes
    # nothing, so retrying smaller is safe.)
    remaining = list(answers)
    posted = 0
    batch = 25
    while remaining and batch >= 1:
        chunk = remaining[:batch]
        try:
            env.post_answers(chunk)
            posted += len(chunk)
            remaining = remaining[batch:]
            batch = 25
        except IOError as e:
            _log("post batch of %d refused: %s" % (len(chunk), e))
            if batch == 1:
                # This one answer is bad; drop it and move on.
                _log("dropping unpostable answer for %s" % chunk[0]["id"])
                remaining = remaining[1:]
                batch = 25
            else:
                batch = max(1, batch // 2)
    _log("posted %d of %d answers" % (posted, len(answers)))
    return 0 if posted == len(answers) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log("fatal: %s" % e)
        sys.exit(1)
