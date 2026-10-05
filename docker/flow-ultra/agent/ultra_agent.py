#!/usr/bin/env python3
"""Ultra host agent: runs on the browser host (cf-worker-01) as systemd unit `flow-ultra-agent`.

Pull model (tmp/ultra_browsers_plan.md): every POLL_S seconds it POSTs the observations of every browser it
manages to {FLOW_BASE}/api/ultra/agent/poll (Bearer ULTRA_AGENT_TOKEN) and gets back the browser list, at most
one job and any challenge replies. Results go to /api/ultra/agent/result. No inbound port on the host.

Jobs (closed list): status, screenshot (read-only), create, bootstrap, login, start, stop, restart, update.
Mutating jobs run one at a time per browser in a worker thread, so the poll loop never blocks (a sign-in may
wait minutes for a phone tap). observe_only browsers (flow-ultra-01/02 until Slice D) get status and
screenshots only. A password or code arrives only in a poll answer, lives in memory for one sign-in, goes to
the in-container helper over stdin, and is never logged or written to disk.

Standard library only (the host runs python3). Docker and CDP go through an injectable Runner so tests run
against a fake. `--dry-run`: observe and report, never take a job, never change anything. `--once`: one poll.
"""
import argparse
import base64
import io
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

AGENT_VERSION = "1.0.0"
HELPER_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fu_cdp.py")
# Static loader on the docker exec command line; the helper source and its parameters come over stdin.
LOADER = ("import sys,json;m=json.loads(sys.stdin.read());"
          "g={'__name__':'fu_helper','CMD':m['cmd'],'PARAMS':m.get('params') or {}};"
          "exec(compile(m['src'],'fu_cdp.py','exec'),g)")
READ_ONLY = ("status", "screenshot")


def log(msg):
    sys.stdout.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")
    sys.stdout.flush()


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.flow_base = (env.get("FLOW_BASE") or "").rstrip("/")
        self.agent_token = env.get("ULTRA_AGENT_TOKEN") or ""
        self.host_id = env.get("HOST_ID") or "cf-worker-01"
        self.image = env.get("FU_IMAGE") or "flow-browser:4"
        self.root = env.get("FU_ROOT") or "/srv/flow-ultra-browsers"
        self.etc = env.get("FU_ETC") or "/etc/flow-ultra-browsers"
        self.poll_s = float(env.get("POLL_S") or 5)
        self.memory = env.get("FU_MEMORY") or "3g"


# ------------------------------------------------------------------------------------------------ runners

class Runner:
    """Runs a command; returns (returncode, stdout, stderr). Tests replace it with a fake."""

    def run(self, argv, input_text=None, timeout=60):
        try:
            p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except FileNotFoundError as e:
            return 127, "", str(e)


class Docker:
    def __init__(self, runner, helper_src=None):
        self.r = runner
        self._src = helper_src

    @property
    def helper_src(self):
        if self._src is None:
            with open(HELPER_SRC_PATH) as f:
                self._src = f.read()
        return self._src

    def names(self, all_=False):
        rc, out, _ = self.r.run(["docker", "ps", "--format", "{{.Names}}"] + (["-a"] if all_ else []), timeout=20)
        return set(out.split()) if rc == 0 else None

    def exec(self, container, argv, timeout=30):
        return self.r.run(["docker", "exec", container] + argv, timeout=timeout)

    def helper(self, container, cmd, params=None, timeout=60):
        """One CDP step inside the container. Parameters (possibly secret) travel on stdin only."""
        payload = json.dumps({"src": self.helper_src, "cmd": cmd, "params": params or {}})
        rc, out, err = self.r.run(["docker", "exec", "-i", container, "python3", "-c", LOADER],
                                  input_text=payload, timeout=timeout)
        line = (out or "").strip().splitlines()[-1:] or [""]
        try:
            res = json.loads(line[0])
        except ValueError:
            raise RuntimeError(f"helper {cmd} failed (rc={rc}): {(err or '')[-200:]}")
        if not res.get("ok"):
            raise RuntimeError(f"helper {cmd}: {res.get('error')}")
        return res.get("result")

    def fu_ready(self, container):
        rc, _, _ = self.exec(container, ["test", "-f", "/tmp/fu-ready"], timeout=15)
        return rc == 0

    def ext_version(self, container):
        rc, out, _ = self.exec(container, ["cat", "/opt/ext/manifest.json"], timeout=15)
        try:
            return json.loads(out).get("version") if rc == 0 else None
        except ValueError:
            return None

    def egress_ip(self, container):
        rc, out, _ = self.exec(container, ["curl", "-fsS", "--max-time", "20", "-x", "http://127.0.0.1:3128",
                                           "https://api.ipify.org"], timeout=30)
        ip = (out or "").strip()
        return ip if rc == 0 and re.fullmatch(r"[0-9a-fA-F.:]{3,45}", ip) else None


class Http:
    def __init__(self, cfg):
        self.cfg = cfg

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(self.cfg.flow_base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.agent_token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def get_bytes(self, url, headers=None, timeout=60):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


# ------------------------------------------------------------------------------------------------ observations

class Observer:
    """Per-browser observations, each with its own timestamp; cheap ones often, expensive ones every 10 min."""
    FAST_S = 60
    SLOW_S = 600

    def __init__(self, docker, clock=time.time):
        self.d = docker
        self.clock = clock
        self.data = {}
        self.last_fast = {}
        self.last_slow = {}
        self.lock = threading.Lock()

    @staticmethod
    def _iso(t):
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))

    def observe(self, browsers, force=False):
        running = self.d.names()
        now = self.clock()
        for b in browsers:
            name, c = b["name"], b["container"]
            o = dict(self.data.get(name, {}))
            if running is None:
                o.update(error="docker ps failed", at=self._iso(now))
                self._store(name, o)
                continue
            up = c in running
            o.update(container_up=up, at=self._iso(now))
            if up and (force or now - self.last_fast.get(name, 0) >= self.FAST_S):
                self.last_fast[name] = now
                o["fu_ready"] = self.d.fu_ready(c)
                o["ext_version"] = self.d.ext_version(c)
            if up and (force or now - self.last_slow.get(name, 0) >= self.SLOW_S):
                self.last_slow[name] = now
                o["egress_ip"] = self.d.egress_ip(c)
                o["egress_at"] = self._iso(now)
                try:
                    o["google_cookies"] = bool(self.d.helper(c, "cookies", timeout=30)["google_signed_in"])
                except Exception as e:
                    o["google_cookies"] = None  # unknown, never "absent"
                    o["error"] = str(e)[:160]
                o["cookies_at"] = self._iso(now)
            if not up:
                o["fu_ready"] = False
            self._store(name, o)

    def _store(self, name, o):
        with self.lock:
            self.data[name] = o

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


# ------------------------------------------------------------------------------------------------ sign-in

def classify(page):
    """What the sign-in page wants. page = {url, text, inputs, buttons} from the helper's `read`."""
    url = (page.get("url") or "").lower()
    text = page.get("text") or ""
    low = text.lower()
    inputs = page.get("inputs") or []
    types = {i.get("type") for i in inputs}
    if url.startswith("chrome://signin-dice-web-intercept") or re.search(r"\bcontinue as\b", low) and "accounts.google.com" not in url:
        return {"step": "chrome_intercept"}
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith("flow.google.com") or host == "labs.google" or host.endswith("myaccount.google.com"):
        return {"step": "signed_in"}
    if host != "accounts.google.com":
        if host.endswith("google.com") and "/signin" not in url:
            return {"step": "signed_in"}
        return {"step": "other", "text": text[:200]}
    if "/signin/rejected" in url or "couldn’t sign you in" in low or "couldn't sign you in" in low or "browser or app may not be secure" in low:
        return {"step": "rejected", "text": text[:200]}
    if "/challenge/recaptcha" in url or "captcha" in low or "type the text you hear or see" in low:
        return {"step": "captcha"}
    if "wrong password" in low:
        return {"step": "wrong_password"}
    if "couldn’t find your google account" in low or "couldn't find your google account" in low:
        return {"step": "wrong_email"}
    if "/challenge/pwd" in url or "password" in types:
        return {"step": "need_password"}
    if "/challenge/dp" in url or "/challenge/ootp" in url or ("gmail app" in low and "tap" in low):
        m = re.search(r"(?:^|\n)\s*(\d{1,3})\s*(?:\n|$)", text)
        dev = re.search(r"(?:gmail app|google app) on (?:your )?([^\n.,]{2,60})", text, re.I)
        return {"step": "challenge", "kind": "number", "number": m.group(1) if m else None,
                "device": dev.group(1).strip() if dev else None}
    if any(k in url for k in ("/challenge/ipp", "/challenge/sms", "/challenge/totp", "/challenge/az", "/challenge/ipe")) \
            or (("tel" in types or "text" in types) and ("code" in low or "g-" in low)):
        hint = re.search(r"(?:sent to|to the number|ending in)\s*([^\n]{2,40})", text, re.I)
        return {"step": "challenge", "kind": "code", "hint": hint.group(1).strip() if hint else "SMS / authenticator"}
    if "choose an account" in low:
        return {"step": "account_chooser"}
    if "email" in types or "/identifier" in url or "/signin/v2/identifier" in url or "/v3/signin/identifier" in url:
        return {"step": "need_email"}
    if any(b.lower() in ("i agree", "not now", "skip", "continue", "confirm") for b in (page.get("buttons") or [])):
        return {"step": "consent"}
    return {"step": "other", "text": text[:200]}


class LoginDriver:
    """One assisted sign-in, bounded: each step has a deadline and a budget; the password and a code are each
    typed once; anything unexpected stops with needs_you (never a retry loop)."""
    MAX_ACTIONS = 30
    MAX_CHALLENGES = 3          # challenge pages per attempt (a new number page = a new challenge)
    ATTEMPT_MAX_S = 1200        # the whole sign-in, every wait included; nothing extends it
    NUMBER_WAIT_S = 300
    CODE_WAIT_S = 600
    STEP_DEADLINE_S = 90
    START_URL = "https://accounts.google.com/signin"
    FLOW_URL = "https://labs.google/fx/tools/flow"

    def __init__(self, docker, container, email, password, attempt_id, report, messages,
                 clock=time.time, sleep=time.sleep):
        self.d, self.c = docker, container
        self.email = email
        self._password = password
        self.attempt_id = attempt_id
        self._report = report         # callable(dict) -> server reply dict (progress)
        self.messages = messages      # queue.Queue of challenge replies for this attempt
        self.clock, self.sleep = clock, sleep
        self.actions = 0
        self.tab = None
        self.typed_email = self.typed_password = False
        self.challenge_id = None
        self.challenges = 0
        self.handled = set()
        self.attempt_deadline = None

    def report(self, progress):
        reply = self._report(progress) or {}
        if reply.get("stop"):
            raise StopSignIn("failed", "flow2api gave up on this sign-in", step="abandoned")
        return reply

    def _act(self):
        self.actions += 1
        if self.actions > self.MAX_ACTIONS:
            raise StopSignIn("needs_you", "too many steps without finishing; stopped")

    def _h(self, cmd, **p):
        return self.d.helper(self.c, cmd, p, timeout=120)

    def _read(self):
        return self._h("read", target_id=self.tab)

    def _intercept_target(self):
        for t in self._h("targets"):
            if t["type"] == "page" and t["url"].startswith("chrome://signin-dice-web-intercept"):
                return t["id"]
        return None

    def run(self):
        try:
            return self._run()
        finally:
            self._password = None

    def _until(self, seconds):
        """A wait's end: never past the attempt-wide deadline."""
        return min(self.clock() + seconds, self.attempt_deadline)

    def _run(self):
        self.attempt_deadline = self.clock() + self.ATTEMPT_MAX_S
        self.tab = self._h("open_tab", url=self.START_URL, background=False)["target_id"]
        self.sleep(4)
        last_url, same_since = None, self.clock()
        while True:
            if self.clock() >= self.attempt_deadline:
                raise StopSignIn("needs_you", f"sign-in did not finish within {self.ATTEMPT_MAX_S // 60} min", step="attempt_timeout")
            it = self._intercept_target()
            if it:
                self._act()
                self._h("click", target_id=it, text="continue as")
                self.sleep(3)
                continue
            page = self._read()
            c = classify(page)
            step = c["step"]
            if page.get("url") != last_url:
                last_url, same_since = page.get("url"), self.clock()
            elif step not in ("challenge",) and self.clock() - same_since > self.STEP_DEADLINE_S:
                raise StopSignIn("needs_you", f"stuck on {step} for {self.STEP_DEADLINE_S}s")
            if step == "signed_in":
                return self._after_google()
            if step == "need_email":
                if self.typed_email:
                    raise StopSignIn("needs_you", "Google asked for the email again")
                self._act()
                self._h("type", target_id=self.tab, selector="input[type=email]", text=self.email)
                self.typed_email = True
                self._next()
            elif step == "need_password":
                if self.typed_password:
                    raise StopSignIn("needs_you", "Google asked for the password again")
                self._act()
                self.report({"step": "password_sent"})   # recorded BEFORE typing: a lost agent = uncertain
                self._h("type", target_id=self.tab, selector="input[type=password]", text=self._password)
                self.typed_password = True
                self._password = None
                self._next()
            elif step == "challenge" and c.get("kind") == "number":
                self._wait_number(c)
            elif step == "challenge" and c.get("kind") == "code":
                self._enter_code(c)
            elif step == "account_chooser":
                if "chooser" in self.handled:
                    raise StopSignIn("needs_you", "account chooser shown twice")
                self.handled.add("chooser")
                self._act()
                self._h("click", target_id=self.tab, text=self.email)
                self.sleep(3)
            elif step == "consent":
                for label in ("not now", "skip", "i agree", "continue", "confirm"):
                    if label not in self.handled and any(b.lower() == label for b in page.get("buttons") or []):
                        self.handled.add(label)
                        self._act()
                        self._h("click", target_id=self.tab, text=label, exact=True)
                        self.sleep(3)
                        break
                else:
                    raise StopSignIn("needs_you", "an unexpected Google page: " + (page.get("text") or "")[:120])
            elif step == "chrome_intercept":
                self._act()
                self._h("click", target_id=self.tab, text="continue as")
                self.sleep(3)
            else:
                detail = {"wrong_password": "Google says the password is wrong", "wrong_email": "Google does not know this email",
                          "captcha": "Google shows a CAPTCHA", "rejected": "Google refused the sign-in from this browser"}
                raise StopSignIn("needs_you", detail.get(step, "an unexpected page: " + (c.get("text") or "")[:120]), step=step)

    def _next(self):
        try:
            self._h("click", target_id=self.tab, text="next", exact=True)
        except RuntimeError:
            self._h("key", target_id=self.tab, key="Enter")
        self.sleep(4)

    def _challenge(self, c):
        self.challenges += 1
        if self.challenges > self.MAX_CHALLENGES:
            raise StopSignIn("needs_you", f"Google asked {self.challenges} challenges in a row; stopped", step="too_many_challenges")
        self._act()
        reply = self.report({"step": "challenge", "challenge": {k: c.get(k) for k in ("kind", "number", "device", "hint")}}) or {}
        self.challenge_id = reply.get("challenge_id")

    def _message(self, wait):
        """The next reply for the CURRENT challenge, else None after `wait` seconds (replies for an older
        challenge are dropped)."""
        try:
            m = self.messages.get_nowait()
        except queue.Empty:
            self.sleep(wait)
            return None
        return m if m.get("challenge_id") == self.challenge_id else None

    def _wait_number(self, c):
        self._challenge(c)
        start_url = self._read().get("url")
        deadline = self._until(self.NUMBER_WAIT_S)
        resends = 0
        while self.clock() < deadline:
            m = self._message(3)
            if m and m["kind"] == "resend" and resends < 2:
                resends += 1
                self._act()
                try:
                    self._h("click", target_id=self.tab, text="resend")
                except RuntimeError:
                    pass
                deadline = self._until(self.NUMBER_WAIT_S)   # bounded: 2 resends, attempt deadline
            if self._intercept_target() or self._read().get("url") != start_url:
                return  # the page moved on ("I tapped it" only makes us look again sooner)
        raise StopSignIn("needs_you", "nobody tapped the number in time; press Retry sign-in", step="challenge_timeout")

    def _enter_code(self, c):
        if "code" in self.handled:
            raise StopSignIn("needs_you", "Google asked for a code again (wrong code?)")
        self._challenge(c)
        deadline = self._until(self.CODE_WAIT_S)
        while self.clock() < deadline:
            m = self._message(5)
            if m and m["kind"] == "code" and m.get("code"):
                self.handled.add("code")
                self._act()
                sel = "input[type=tel], input[name=totpPin], input[type=text]"
                self._h("type", target_id=self.tab, selector=sel, text=m["code"])
                m["code"] = None
                self._next()
                return
        raise StopSignIn("needs_you", "no code entered in time; press Retry sign-in", step="challenge_timeout")

    def _after_google(self):
        self._h("navigate", target_id=self.tab, url=self.FLOW_URL)
        page = {}
        for _ in range(10):
            self.sleep(3)
            page = self._read()
            if "flow.google.com" in (page.get("url") or "") and (page.get("projects") or "ULTRA" in (page.get("text") or "")):
                break
        if "flow.google.com" in (page.get("url") or "") and not page.get("projects") \
                and any(b.lower() == "sign in" for b in page.get("buttons") or []):
            self._act()
            self._h("click", target_id=self.tab, text="sign in", exact=True)
            self.sleep(8)
            page = self._read()
        ultra = "ULTRA" in (page.get("text") or "")
        projects = page.get("projects") or []
        opened = False
        if projects:
            self._h("open_tab", url=projects[0], background=True)   # left open: the extension reads its id
            opened = True
            self._h("close_tab", target_id=self.tab)
        else:
            try:
                self._act()
                self._h("click", target_id=self.tab, text="new project")
                self.sleep(8)
                opened = "/project/" in (self._read().get("url") or "")   # this tab IS the project now: keep it
            except RuntimeError:
                opened = False
        return {"step": "signed_in", "ultra": ultra, "project_opened": opened}


class StopSignIn(Exception):
    def __init__(self, outcome, detail, step="stopped"):
        super().__init__(detail)
        self.outcome, self.detail, self.step = outcome, detail, step


# ------------------------------------------------------------------------------------------------ jobs

class Jobs:
    def __init__(self, cfg, docker, http, runner, observer, clock=time.time, sleep=time.sleep):
        self.cfg, self.d, self.http, self.r, self.obs = cfg, docker, http, runner, observer
        self.clock, self.sleep = clock, sleep
        self.inboxes = {}  # attempt_id -> queue.Queue

    # paths for a managed browser
    def paths(self, name):
        base = os.path.join(self.cfg.root, name)
        return {"base": base, "profile": os.path.join(base, "profile"), "releases": os.path.join(base, "releases"),
                "etc": os.path.join(self.cfg.etc, name), "site": os.path.join(self.cfg.etc, name, "site.json")}

    def _must(self, argv, timeout=120):
        rc, out, err = self.r.run(argv, timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {(err or out or '')[-200:]}")
        return out

    def wait_ready(self, container, version=None, seconds=90):
        end = self.clock() + seconds
        while self.clock() < end:
            self.sleep(2)
            if self.d.fu_ready(container) and (version is None or self.d.ext_version(container) == version.split("-")[0]):
                return True
        return False

    # --- extension release (same steps as ext-sync.sh) ---
    def _download_release(self, conn_token):
        hdr = {"Authorization": f"Bearer {conn_token}"}
        latest = json.loads(self.http.get_bytes(self.cfg.flow_base + "/api/plugin/ext-version", headers=hdr, timeout=20)).get("version")
        if not latest:
            raise RuntimeError("server did not report an extension version")
        # header auth only: a ?token= query would be written into the server's access log
        raw = self.http.get_bytes(self.cfg.flow_base + "/download/worker-latest.zip", headers=hdr, timeout=60)
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
        manifests = sorted([n for n in names if n.rsplit("/", 1)[-1] == "manifest.json"], key=lambda n: n.count("/"))
        if not manifests:
            raise RuntimeError("package has no manifest.json")
        prefix = manifests[0][: -len("manifest.json")]
        files = {n[len(prefix):]: z.read(n) for n in names if n.startswith(prefix) and not n.endswith("/")}
        got = json.loads(files["manifest.json"]).get("version")
        if got != latest:
            raise RuntimeError(f"downloaded {got} but the server advertises {latest}")
        for need in ("manifest.json", "background.js", "options.html", "options.js"):
            if need not in files:
                raise RuntimeError(f"package lacks {need}")
        if "site.json" in files or "site.js" in files:
            raise RuntimeError("published package contains site.json/site.js; refusing")
        if any(".." in n.split("/") or n.startswith("/") for n in files):
            raise RuntimeError("package has unsafe paths")
        return latest, files

    def _install_release(self, name, version, files, site):
        p = self.paths(name)
        dest = os.path.join(p["releases"], version)
        if os.path.exists(dest):
            dest = f"{dest}-{int(self.clock())}"
        tmp = dest + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, data in files.items():
            fp = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(data)
        site_json = json.dumps(site)
        for fn, body in (("site.json", site_json), ("site.js", f"globalThis.FlowSite = {site_json};\n")):
            with open(os.path.join(tmp, fn), "w") as f:
                f.write(body)
        # The service runs with UMask=0077, so every mode is set explicitly: the container (uid 1000) must
        # traverse and read the release. Code is 0755/0644; site.json/site.js hold the proxy password: 0400,
        # owned by uid 1000 (nobody else is a user on the box).
        for dirpath, dirnames, filenames in os.walk(tmp):
            os.chmod(dirpath, 0o755)
            for fn in filenames:
                os.chmod(os.path.join(dirpath, fn), 0o644)
        self._must(["chown", "-R", "1000:1000", tmp])
        for fn in ("site.json", "site.js"):
            os.chmod(os.path.join(tmp, fn), 0o400)
        os.rename(tmp, dest)
        return os.path.basename(dest)

    def _set_current(self, name, version):
        p = self.paths(name)
        cur = os.path.join(p["releases"], "current")
        with open(cur + ".tmp", "w") as f:
            f.write(version)
        os.chmod(cur + ".tmp", 0o644)   # the entrypoint (uid 1000) reads it first thing; UMask=0077 would hide it
        os.replace(cur + ".tmp", cur)

    def _write_site(self, name, site):
        p = self.paths(name)
        os.makedirs(p["etc"], mode=0o700, exist_ok=True)
        os.chmod(p["etc"], 0o700)
        fd = os.open(p["site"] + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(site, f)
        os.replace(p["site"] + ".tmp", p["site"])

    # --- job kinds ---
    def create(self, b, secrets):
        name, c = b["name"], b["container"]
        existing = self.d.names(all_=True)
        if existing is None:
            raise RuntimeError("docker ps failed")
        if c in existing:
            raise RuntimeError(f"container {c} already exists (never removed automatically)")
        p = self.paths(name)
        if os.path.exists(os.path.join(p["profile"], "browser")):
            raise RuntimeError(f"{p['profile']} already holds a profile; refusing to reuse it")
        for d in (p["profile"], p["releases"]):
            os.makedirs(d, exist_ok=True)
        os.chmod(p["releases"], 0o755)   # mounted read-only at /opt/releases; uid 1000 lists and reads it
        os.chmod(p["profile"], 0o700)
        self._must(["chown", "1000:1000", p["profile"]])
        site = secrets["site"]
        self._write_site(name, site)
        version, files = self._download_release(secrets["connection_token"])
        rel = self._install_release(name, version, files, site)
        self._set_current(name, rel)
        argv = ["docker", "run", "-d", "--name", c, "--hostname", c, "--restart=unless-stopped", "--network", "bridge",
                "--shm-size=1g", f"--memory={self.cfg.memory}", "-e", "FU_REQUIRE_PROXY=1"]
        if b.get("timezone"):
            argv += ["-e", f"TZ={b['timezone']}"]
        argv += ["-v", f"{p['profile']}:/profile", "-v", f"{p['releases']}:/opt/releases:ro", self.cfg.image]
        self._must(argv)
        if not self.wait_ready(c, version):
            raise RuntimeError("container did not become ready in 90 s")
        return {"ready": True, "version": version}

    def update(self, b, secrets):
        name, c = b["name"], b["container"]
        if b.get("desired_state") != "running":
            raise RuntimeError("browser is stopped; updates only run on a running browser")
        p = self.paths(name)
        version, files = self._download_release(secrets["connection_token"])
        site = secrets["site"]
        self._write_site(name, site)
        cur = ""
        try:
            cur = open(os.path.join(p["releases"], "current")).read().strip()
        except OSError:
            pass
        if cur.split("-")[0] == version and self.d.ext_version(c) == version:
            return {"ready": self.d.fu_ready(c), "version": version, "changed": False}
        rel = self._install_release(name, version, files, site)
        with open(os.path.join(p["releases"], "pending"), "w") as f:
            f.write(rel)
        self._set_current(name, rel)
        self._must(["docker", "restart", "-t", "30", c])
        if not self.wait_ready(c, version, seconds=60):
            raise RuntimeError(f"container did not come up on {version} in 60 s (previous release kept, pending set)")
        os.remove(os.path.join(p["releases"], "pending"))
        keep = {rel, cur}
        for d in os.listdir(p["releases"]):
            full = os.path.join(p["releases"], d)
            if os.path.isdir(full) and d not in keep and not d.endswith(".new"):
                shutil.rmtree(full, ignore_errors=True)
        return {"ready": True, "version": version, "changed": True}

    def start(self, b, secrets):
        self._must(["docker", "start", b["container"]])
        return {"ready": self.wait_ready(b["container"])}

    def stop(self, b, secrets):
        self._must(["docker", "stop", "-t", "30", b["container"]], timeout=60)
        return {"stopped": True}

    def restart(self, b, secrets):
        self._must(["docker", "restart", "-t", "30", b["container"]], timeout=90)
        return {"ready": self.wait_ready(b["container"])}

    def bootstrap(self, b, secrets):
        out = self.d.helper(b["container"], "ext_bootstrap", {
            "serverBase": self.cfg.flow_base, "connectionToken": secrets["connection_token"], "routeKey": secrets["route_key"]},
            timeout=60)
        if not out or not out.get("ok"):
            raise RuntimeError(f"bootstrap refused: {(out or {}).get('error')}")
        return {"route_key": out.get("routeKey"), "via": out.get("via"), "version": out.get("version")}

    def status(self, b, secrets):
        self.obs.observe([b], force=True)
        return {"observed": True}

    def screenshot(self, b, secrets):
        out = self.d.helper(b["container"], "screenshot", {}, timeout=60)
        return {"png_b64": out["png_b64"]}

    def login(self, b, secrets, job, report):
        inbox = self.inboxes.setdefault(job["attempt_id"], queue.Queue())
        drv = LoginDriver(self.d, b["container"], secrets["email"], secrets["password"], job["attempt_id"],
                          report, inbox, clock=self.clock, sleep=self.sleep)
        secrets["password"] = None
        try:
            return "done", drv.run()
        except StopSignIn as s:
            return "failed", {"step": s.step, "detail": s.detail}
        except Exception as e:
            # the helper or Chrome broke mid-way: after the password went out nobody knows what Google saw
            return ("uncertain" if drv.typed_password else "failed"), {"step": "error", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            self.inboxes.pop(job["attempt_id"], None)

    def deliver(self, message):
        q = self.inboxes.get(message.get("attempt_id"))
        if q is not None:
            q.put(message)


# ------------------------------------------------------------------------------------------------ agent loop

class Agent:
    def __init__(self, cfg, runner=None, http=None, clock=time.time, sleep=time.sleep, dry_run=False):
        self.cfg = cfg
        self.runner = runner or Runner()
        self.http = http or Http(cfg)
        self.docker = Docker(self.runner)
        self.observer = Observer(self.docker, clock=clock)
        self.jobs = Jobs(cfg, self.docker, self.http, self.runner, self.observer, clock=clock, sleep=sleep)
        self.dry_run = dry_run
        self.browsers = []
        self.running = {}   # job_id -> thread
        self.busy = set()   # browser names with a mutating job running
        self.lock = threading.Lock()
        self.clock = clock

    def observe(self):
        try:
            self.observer.observe(list(self.browsers))
        except Exception as e:
            log(f"observe failed: {type(e).__name__}: {e}")

    def poll_once(self, observe=True):
        if observe:
            self.observe()
        with self.lock:
            running = list(self.running)
        body = {"host_id": self.cfg.host_id, "agent_version": AGENT_VERSION, "observations": self.observer.snapshot(),
                "running_jobs": running, "accept_jobs": not self.dry_run}
        reply = self.http.post("/api/ultra/agent/poll", body)
        self.browsers = [b for b in reply.get("browsers") or [] if isinstance(b, dict) and b.get("name")]
        for m in reply.get("messages") or []:
            self.jobs.deliver(m)
        job = reply.get("job")
        if job and not self.dry_run:
            self.start_job(job)
        return reply

    def _report(self, job_id, outcome, result):
        for attempt in range(3):
            try:
                return self.http.post("/api/ultra/agent/result",
                                      {"host_id": self.cfg.host_id, "job_id": job_id, "outcome": outcome, "result": result})
            except Exception as e:
                log(f"result post failed for {job_id[:8]} ({type(e).__name__}); retry {attempt + 1}")
                time.sleep(2)
        return {}

    def start_job(self, job):
        b = next((x for x in self.browsers if x["name"] == job["browser"]), None)
        kind = job["kind"]
        secrets = job.pop("secrets", None) or {}
        if b is None:
            self._report(job["id"], "failed", {"error": "browser not in this agent's list"})
            return
        if b.get("mode") != "managed" and kind not in READ_ONLY:
            self._report(job["id"], "failed", {"error": "observe-only browser: refused"})
            return
        if kind not in READ_ONLY:
            with self.lock:
                if b["name"] in self.busy:
                    self._report(job["id"], "failed", {"error": "another operation is running on this browser"})
                    return
                self.busy.add(b["name"])
        t = threading.Thread(target=self._run_job, args=(job, b, kind, secrets), daemon=True, name=f"job-{kind}-{b['name']}")
        with self.lock:
            self.running[job["id"]] = t
        t.start()

    def _run_job(self, job, b, kind, secrets):
        log(f"{b['name']}: {kind} started ({job['id'][:8]})")
        outcome, result = "failed", {}
        try:
            if kind == "login":
                outcome, result = self.jobs.login(b, secrets, job, lambda r: self._report(job["id"], "progress", r))
            else:
                fn = getattr(self.jobs, kind, None)
                if fn is None:
                    raise RuntimeError(f"unknown job kind {kind}")
                result = fn(b, secrets) or {}
                outcome = "done"
        except Exception as e:
            # flow2api keeps the account held after any failed lifecycle step, so "failed" is enough here;
            # only a sign-in distinguishes "uncertain" (see Jobs.login)
            outcome = "failed"
            result = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        finally:
            secrets.clear()
            with self.lock:
                self.running.pop(job["id"], None)
                self.busy.discard(b["name"])
        log(f"{b['name']}: {kind} {outcome}" + (f" ({result.get('error') or result.get('detail')})" if outcome != "done" else ""))
        self._report(job["id"], outcome, result)

    def _observe_forever(self):
        while True:
            self.observe()
            time.sleep(self.cfg.poll_s)

    def loop(self, once=False):
        # Observations (docker exec, a CDP cookie read every 10 min) run in their own thread: a slow one never
        # delays the poll that renews a waiting sign-in's lease.
        threading.Thread(target=self._observe_forever, daemon=True, name="observer").start()
        while True:
            try:
                self.poll_once(observe=False)
            except urllib.error.HTTPError as e:
                log(f"poll refused: HTTP {e.code}")
            except Exception as e:
                log(f"poll failed: {type(e).__name__}: {e}")
            if once:
                return
            time.sleep(self.cfg.poll_s + random.uniform(0, 0.5))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ultra browsers host agent")
    ap.add_argument("--dry-run", action="store_true", help="observe and report only: never take a job, change nothing")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    args = ap.parse_args(argv)
    cfg = Config()
    if not cfg.flow_base or not cfg.agent_token:
        sys.stderr.write("FLOW_BASE and ULTRA_AGENT_TOKEN are required (see flow-ultra-agent.env.example)\n")
        return 2
    log(f"ultra agent {AGENT_VERSION} host={cfg.host_id} base={cfg.flow_base} dry_run={args.dry_run}")
    agent = Agent(cfg, dry_run=args.dry_run)
    if args.once:
        for _ in range(2):   # the first poll only learns the browser list
            try:
                agent.poll_once()
            except Exception as e:
                log(f"poll failed: {type(e).__name__}: {e}")
                return 1
        if args.dry_run:
            print(json.dumps({"browsers": agent.browsers, "observations": agent.observer.snapshot()}, indent=2))
        return 0
    agent.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
