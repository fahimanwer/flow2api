"""Ultra host agent (docker/flow-ultra/agent/ultra_agent.py) against a fake docker/CDP layer: page
classification, the bounded sign-in driver, secrets only on stdin, provisioning files and flags, dry-run,
observe-only refusal, and the entrypoint's FU_REQUIRE_PROXY check."""
import importlib.util
import io
import json
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ultra_agent", ROOT / "docker/flow-ultra/agent/ultra_agent.py")
ua = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ua)

PASSWORD = "Pw-never-in-argv-42"


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDocker:
    """Scripted pages: each `read` returns the current page; actions move the script forward."""

    def __init__(self, pages, on_action=None):
        self.pages = pages
        self.i = 0
        self.calls = []
        self.on_action = on_action or (lambda d, cmd, p: None)
        self.intercept = False

    def helper(self, container, cmd, params=None, timeout=60):
        self.calls.append((cmd, dict(params or {})))
        if cmd == "open_tab":
            return {"target_id": "T1" if "accounts" in params.get("url", "") else "T2"}
        if cmd == "targets":
            return [{"id": "I", "type": "page", "url": "chrome://signin-dice-web-intercept/"}] if self.intercept else []
        if cmd == "read":
            return self.pages[min(self.i, len(self.pages) - 1)]
        self.on_action(self, cmd, params or {})
        return {}


def page(url, text="", inputs=(), buttons=(), projects=()):
    return {"url": url, "text": text, "inputs": [{"type": t} for t in inputs], "buttons": list(buttons), "projects": list(projects)}


IDENT = page("https://accounts.google.com/v3/signin/identifier?x", "Sign in\nEmail or phone", ["email"], ["Next"])
PWD = page("https://accounts.google.com/v3/signin/challenge/pwd", "Welcome\nEnter your password", ["password"], ["Next"])
DP = page("https://accounts.google.com/v3/signin/challenge/dp?x", "2-Step Verification\nOpen the Gmail app on iPhone 13\nTap Yes on the prompt, then tap\n11\non your phone")
FLOW = page("https://flow.google.com/", "Flow ULTRA\nNew project", [], ["New project"], ["https://flow.google.com/project/abc"])


class ClassifyTests(unittest.TestCase):
    def test_pages(self):
        self.assertEqual(ua.classify(IDENT)["step"], "need_email")
        self.assertEqual(ua.classify(PWD)["step"], "need_password")
        c = ua.classify(DP)
        self.assertEqual((c["step"], c["kind"], c["number"], c["device"]), ("challenge", "number", "11", "iPhone 13"))
        c = ua.classify(page("https://accounts.google.com/v3/signin/challenge/ipp", "Enter the code sent to (•••) •••-••42", ["tel"]))
        self.assertEqual((c["step"], c["kind"]), ("challenge", "code"))
        self.assertEqual(ua.classify(page("https://accounts.google.com/v3/signin/challenge/pwd", "Wrong password. Try again", ["password"]))["step"], "wrong_password")
        self.assertEqual(ua.classify(page("https://accounts.google.com/v3/signin/challenge/recaptcha", "Confirm you're not a robot"))["step"], "captcha")
        self.assertEqual(ua.classify(page("https://accounts.google.com/v3/signin/rejected", "Couldn't sign you in"))["step"], "rejected")
        self.assertEqual(ua.classify(page("chrome://signin-dice-web-intercept/", "Continue as Ana"))["step"], "chrome_intercept")
        self.assertEqual(ua.classify(FLOW)["step"], "signed_in")
        self.assertEqual(ua.classify(page("https://myaccount.google.com/"))["step"], "signed_in")
        self.assertEqual(ua.classify(page("https://accounts.google.com/speedbump", "Something new", [], ["Not now"]))["step"], "consent")
        self.assertEqual(ua.classify(page("https://accounts.google.com/v3/signin/challenge/xyz", "???"))["step"], "other")


class LoginDriverTests(unittest.TestCase):
    def _driver(self, docker, msgs=None, reports=None):
        clock = FakeClock()
        reports = [] if reports is None else reports

        def report(r):
            reports.append(json.loads(json.dumps(r)))
            return {"challenge_id": "C1"} if r.get("step") == "challenge" else {}
        return ua.LoginDriver(docker, "flow-ultra-03", "new@x.com", PASSWORD, "A1", report, msgs or queue.Queue(),
                              clock=clock, sleep=clock.sleep), reports

    def test_happy_path_number_tap_intercept_project(self):
        def act(d, cmd, p):
            if cmd == "type" and d.i == 0:
                return
            if cmd in ("click", "key") and d.i in (0, 1):
                d.i += 1
            if cmd == "navigate":
                d.i = 4
        docker = FakeDocker([IDENT, PWD, DP, DP, FLOW], act)
        drv, reports = self._driver(docker)
        # the phone tap happens while the agent watches the challenge
        orig = docker.helper

        def helper(c, cmd, params=None, timeout=60):
            if cmd == "read" and docker.i == 2 and sum(1 for x in docker.calls if x[0] == "read") > 4:
                docker.i, docker.intercept = 3, True
            if cmd == "click" and (params or {}).get("target_id") == "I":
                docker.intercept = False
                docker.i = 4
            return orig(c, cmd, params, timeout)
        docker.helper = helper
        out = drv.run()
        self.assertEqual(out, {"step": "signed_in", "ultra": True, "project_opened": True})
        typed = [p["text"] for cmd, p in docker.calls if cmd == "type"]
        self.assertEqual(typed, ["new@x.com", PASSWORD])
        self.assertEqual([r["step"] for r in reports], ["password_sent", "challenge"])
        self.assertEqual(reports[1]["challenge"]["number"], "11")
        pw_idx = next(i for i, (c, p) in enumerate(docker.calls) if c == "type" and p["text"] == PASSWORD)
        self.assertTrue(pw_idx > 0)
        self.assertIsNone(drv._password)
        self.assertIn(("open_tab", {"url": "https://flow.google.com/project/abc", "background": True}), docker.calls)

    def test_wrong_password_stops_without_retyping(self):
        wrong = page("https://accounts.google.com/v3/signin/challenge/pwd", "Wrong password. Try again", ["password"])

        def act(d, cmd, p):
            if cmd in ("click", "key"):
                d.i += 1
        drv, _ = self._driver(FakeDocker([IDENT, PWD, wrong], act))
        with self.assertRaises(ua.StopSignIn) as e:
            drv.run()
        self.assertEqual(e.exception.step, "wrong_password")
        self.assertEqual(sum(1 for c, p in drv.d.calls if c == "type" and p["text"] == PASSWORD), 1)

    def test_code_challenge_uses_only_a_reply_for_the_current_challenge(self):
        code_page = page("https://accounts.google.com/v3/signin/challenge/ipp", "Enter the code sent to ••42", ["tel"], ["Next"])
        msgs = queue.Queue()
        msgs.put({"challenge_id": "OLD", "kind": "code", "code": "999999"})
        msgs.put({"challenge_id": "C1", "kind": "code", "code": "123456"})

        def act(d, cmd, p):
            if cmd in ("click", "key"):
                d.i += 1
            if cmd == "navigate":
                d.i = 4
        drv, _ = self._driver(FakeDocker([IDENT, PWD, code_page, FLOW, FLOW], act), msgs)
        out = drv.run()
        self.assertEqual(out["step"], "signed_in")
        typed = [p["text"] for c, p in drv.d.calls if c == "type"]
        self.assertEqual(typed, ["new@x.com", PASSWORD, "123456"])

    def test_number_not_tapped_times_out_to_needs_you(self):
        def act(d, cmd, p):
            if cmd in ("click", "key"):
                d.i += 1
        drv, _ = self._driver(FakeDocker([IDENT, PWD, DP], act))
        with self.assertRaises(ua.StopSignIn) as e:
            drv.run()
        self.assertEqual(e.exception.step, "challenge_timeout")

    def test_alternating_challenges_are_bounded(self):
        """REGRESSION (code review #5): a new number page after every look must not wait forever."""
        dp2 = dict(DP, url=DP["url"] + "&again=1")

        class Flipper(FakeDocker):
            def helper(self, container, cmd, params=None, timeout=60):
                if cmd == "read" and self.i >= 2:
                    self.n = getattr(self, "n", 0) + 1
                    return DP if self.n % 2 else dp2
                return super().helper(container, cmd, params, timeout)
        drv, reports = self._driver(Flipper([IDENT, PWD], lambda d, c, p: setattr(d, "i", d.i + (c in ("click", "key")))))
        start = drv.clock()
        with self.assertRaises(ua.StopSignIn) as e:
            drv.run()
        self.assertEqual(e.exception.step, "too_many_challenges")
        self.assertLessEqual(sum(1 for r in reports if r["step"] == "challenge"), ua.LoginDriver.MAX_CHALLENGES)
        self.assertLess(drv.clock() - start, ua.LoginDriver.ATTEMPT_MAX_S)

    def test_attempt_deadline_caps_resends(self):
        msgs = queue.Queue()
        drv, reports = self._driver(FakeDocker([IDENT, PWD, DP], lambda d, c, p: setattr(d, "i", d.i + (c in ("click", "key")))), msgs)
        drv.ATTEMPT_MAX_S = 400
        orig_report = drv._report

        def report(r):
            out = orig_report(r)
            if r.get("step") == "challenge":
                for _ in range(2):
                    msgs.put({"challenge_id": "C1", "kind": "resend"})
            return out
        drv._report = report
        start = drv.clock()
        with self.assertRaises(ua.StopSignIn):
            drv.run()
        self.assertLessEqual(drv.clock() - start, 400 + 10)

    def test_flow2api_giving_up_stops_the_driver(self):
        drv, _ = self._driver(FakeDocker([IDENT, PWD], lambda d, c, p: setattr(d, "i", d.i + (c in ("click", "key")))))
        drv._report = lambda r: {"ok": False, "stop": True}
        with self.assertRaises(ua.StopSignIn):
            drv.run()
        self.assertNotIn(PASSWORD, [p.get("text") for c, p in drv.d.calls if c == "type"])


class FakeRunner:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run(self, argv, input_text=None, timeout=60):
        self.calls.append((list(argv), input_text))
        key = " ".join(argv[:3])
        for prefix, resp in self.responses.items():
            if key.startswith(prefix):
                return resp(argv, input_text) if callable(resp) else resp
        return 0, "", ""


class DockerHelperTests(unittest.TestCase):
    def test_secrets_travel_on_stdin_only(self):
        r = FakeRunner({"docker exec -i": (0, json.dumps({"ok": True, "result": {"typed": 3}}) + "\n", "")})
        d = ua.Docker(r, helper_src="print('x')")
        self.assertEqual(d.helper("flow-ultra-03", "type", {"text": PASSWORD}), {"typed": 3})
        argv, stdin = r.calls[0]
        self.assertNotIn(PASSWORD, " ".join(argv))
        self.assertIn(PASSWORD, stdin)
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "flow-ultra-03"])
        r2 = FakeRunner({"docker exec -i": (0, json.dumps({"ok": False, "error": "field not found"}), "")})
        with self.assertRaises(RuntimeError):
            ua.Docker(r2, helper_src="").helper("c", "type", {"text": PASSWORD})

    def test_loader_runs_the_helper_source(self):
        src = "import json,sys\nsys.stdout.write(json.dumps({'ok': True, 'result': [CMD, PARAMS]}) + '\\n')\n"
        p = subprocess.run([sys.executable, "-c", ua.LOADER], input=json.dumps({"src": src, "cmd": "c1", "params": {"a": 1}}),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(json.loads(p.stdout)["result"], ["c1", {"a": 1}])


class HelperSourceTests(unittest.TestCase):
    def test_real_helper_answers_one_json_line_even_without_chrome(self):
        src = (ROOT / "docker/flow-ultra/agent/fu_cdp.py").read_text()
        p = subprocess.run([sys.executable, "-c", ua.LOADER], input=json.dumps({"src": src.replace("127.0.0.1:9222", "127.0.0.1:9"),
                           "cmd": "type", "params": {"text": PASSWORD, "target_id": "x", "selector": "input"}}),
                           capture_output=True, text=True, timeout=20)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertFalse(out["ok"])
        self.assertNotIn(PASSWORD, p.stdout + p.stderr)


class FakeHttp:
    def __init__(self, version="3.7.5"):
        self.version = version
        self.posts = []
        self.replies = []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"version": version}))
            for f in ("background.js", "options.html", "options.js"):
                z.writestr(f, "//")
        self.zip = buf.getvalue()

    def post(self, path, body, timeout=30):
        self.posts.append((path, json.loads(json.dumps(body))))
        return self.replies.pop(0) if self.replies else {"browsers": [], "job": None, "messages": []}

    def get_bytes(self, url, headers=None, timeout=60):
        if url.endswith("/api/plugin/ext-version"):
            return json.dumps({"version": self.version}).encode()
        return self.zip


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ua.Config({"FLOW_BASE": "https://flow.example.com", "ULTRA_AGENT_TOKEN": "a", "FU_ROOT": self.tmp.name + "/srv",
                              "FU_ETC": self.tmp.name + "/etc", "FU_IMAGE": "flow-browser:4"})
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def _jobs(self, runner):
        d = ua.Docker(runner, helper_src="")
        return ua.Jobs(self.cfg, d, FakeHttp(), runner, ua.Observer(d), clock=self.clock, sleep=self.clock.sleep)

    def test_create_writes_protected_files_and_runs_a_proxy_required_container(self):
        r = FakeRunner({
            "docker ps --format": (0, "flow-ultra-01\n", ""),
            "docker exec flow-ultra-03": lambda argv, _: (0, json.dumps({"version": "3.7.5"}), "") if "cat" in argv else (0, "", ""),
        })
        site = {"proxyUrl": "http://u:p@disp.oxylabs.io:8011", "clientLabel": "flow-ultra-03", "proxyAllHosts": True}
        out = self._jobs(r).create({"name": "flow-ultra-03", "container": "flow-ultra-03", "timezone": "America/New_York"},
                                   {"site": site, "connection_token": "conn"})
        self.assertEqual(out["ready"], True)
        etc_site = Path(self.cfg.etc) / "flow-ultra-03" / "site.json"
        self.assertEqual(stat.S_IMODE(etc_site.stat().st_mode), 0o600)
        rel = Path(self.cfg.root) / "flow-ultra-03" / "releases"
        self.assertEqual((rel / "current").read_text(), "3.7.5")
        self.assertEqual(stat.S_IMODE((rel / "3.7.5" / "site.js").stat().st_mode), 0o400)
        self.assertIn("globalThis.FlowSite", (rel / "3.7.5" / "site.js").read_text())
        run = next(a for a, _ in r.calls if a[:2] == ["docker", "run"])
        for flag in ("--restart=unless-stopped", "FU_REQUIRE_PROXY=1", "TZ=America/New_York", "flow-browser:4"):
            self.assertIn(flag, run)
        self.assertFalse(any("p@disp" in " ".join(a) for a, _ in r.calls), "proxy credentials on a command line")
        self.assertTrue(any(a[:2] == ["chown", "-R"] for a, _ in r.calls))

    def test_container_user_can_read_its_release_under_the_service_umask(self):
        """REGRESSION (code review #3): the unit runs with UMask=0077 as root; the container runs as uid 1000.
        Real files, real modes; ownership is what the agent's chown calls give (a non-root test cannot chown)."""
        r = FakeRunner({
            "docker ps --format": (0, "", ""),
            "docker exec flow-ultra-03": lambda argv, _: (0, json.dumps({"version": "3.7.5"}), "") if "cat" in argv else (0, "", ""),
        })
        site = {"proxyUrl": "http://u:p@disp.oxylabs.io:8011", "clientLabel": "flow-ultra-03", "proxyAllHosts": True}
        old = os.umask(0o077)
        try:
            self._jobs(r).create({"name": "flow-ultra-03", "container": "flow-ultra-03"}, {"site": site, "connection_token": "conn"})
        finally:
            os.umask(old)
        chowned = [Path(a[-1]) for a, _ in r.calls if a[0] == "chown"]
        recursive = [Path(a[-1]) for a, _ in r.calls if a[:2] == ["chown", "-R"]]

        def owner_is_1000(path):
            return any(path == c for c in chowned) or any(c in path.parents or c == path for c in recursive) \
                or any(str(path).startswith(str(c).replace(".new", "")) for c in recursive)

        def uid1000_can(path, need_x=False):
            mode = stat.S_IMODE(path.stat().st_mode)
            bits = (0o500 if need_x else 0o400) if owner_is_1000(path) else (0o005 if need_x else 0o004)
            return mode & bits == bits

        rel = Path(self.cfg.root) / "flow-ultra-03" / "releases"
        self.assertTrue(uid1000_can(rel, need_x=True), oct(rel.stat().st_mode))
        self.assertTrue(uid1000_can(rel / "current"), oct((rel / "current").stat().st_mode))
        release = rel / (rel / "current").read_text()
        for path in [release, *release.rglob("*")]:
            self.assertTrue(uid1000_can(path, need_x=path.is_dir()), f"{path} {oct(path.stat().st_mode)}")
        for secret in ("site.json", "site.js"):   # readable by uid 1000 ONLY
            self.assertEqual(stat.S_IMODE((release / secret).stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((Path(self.cfg.etc) / "flow-ultra-03" / "site.json").stat().st_mode), 0o600)

    def test_download_sends_the_connection_token_only_in_a_header(self):
        """REGRESSION (code review #4): no ?token= in any URL (the server's access log keeps the query)."""
        http = FakeHttp()
        seen = []
        orig = http.get_bytes

        def get_bytes(url, headers=None, timeout=60):
            seen.append((url, dict(headers or {})))
            return orig(url, headers, timeout)
        http.get_bytes = get_bytes
        d = ua.Docker(FakeRunner(), helper_src="")
        jobs = ua.Jobs(self.cfg, d, http, FakeRunner(), ua.Observer(d), clock=self.clock, sleep=self.clock.sleep)
        jobs._download_release("conn-secret")
        self.assertTrue(seen)
        for url, hdr in seen:
            self.assertNotIn("conn-secret", url)
            self.assertNotIn("token=", url)
            self.assertEqual(hdr.get("Authorization"), "Bearer conn-secret")

    def test_create_refuses_an_existing_container(self):
        r = FakeRunner({"docker ps --format": (0, "flow-ultra-03\n", "")})
        with self.assertRaises(RuntimeError):
            self._jobs(r).create({"name": "flow-ultra-03", "container": "flow-ultra-03"}, {"site": {}, "connection_token": "c"})
        self.assertFalse(any(a[:2] == ["docker", "run"] for a, _ in r.calls))

    def test_update_refuses_a_stopped_browser(self):
        with self.assertRaises(RuntimeError):
            self._jobs(FakeRunner()).update({"name": "x", "container": "x", "desired_state": "stopped"}, {"site": {}, "connection_token": "c"})


class AgentTests(unittest.TestCase):
    def _agent(self, dry_run=False):
        cfg = ua.Config({"FLOW_BASE": "https://flow.example.com", "ULTRA_AGENT_TOKEN": "a", "HOST_ID": "h1"})
        http = FakeHttp()
        r = FakeRunner({"docker ps --format": (0, "flow-ultra-01\n", "")})
        agent = ua.Agent(cfg, runner=r, http=http, dry_run=dry_run)
        agent.docker._src = ""
        return agent, http, r

    def test_dry_run_never_accepts_a_job(self):
        agent, http, _ = self._agent(dry_run=True)
        http.replies = [{"browsers": [{"name": "flow-ultra-01", "container": "flow-ultra-01", "mode": "observe_only"}],
                         "job": {"id": "j1", "browser": "flow-ultra-01", "kind": "restart"}, "messages": []}]
        agent.poll_once()
        self.assertIs(http.posts[0][1]["accept_jobs"], False)
        self.assertEqual(agent.running, {})
        self.assertFalse(any(p == "/api/ultra/agent/result" for p, _ in http.posts))

    def test_observe_only_browser_refuses_lifecycle_jobs(self):
        agent, http, r = self._agent()
        agent.browsers = [{"name": "flow-ultra-01", "container": "flow-ultra-01", "mode": "observe_only"}]
        agent.start_job({"id": "j1", "browser": "flow-ultra-01", "kind": "restart"})
        self.assertEqual(http.posts[-1][1]["outcome"], "failed")
        self.assertFalse(any(a[:2] == ["docker", "restart"] for a, _ in r.calls))

    def test_poll_reports_observations_and_running_jobs(self):
        agent, http, _ = self._agent()
        agent.browsers = [{"name": "flow-ultra-01", "container": "flow-ultra-01", "mode": "observe_only"}]
        agent.poll_once()
        body = http.posts[0][1]
        self.assertEqual(body["host_id"], "h1")
        self.assertIn("flow-ultra-01", body["observations"])
        self.assertTrue(body["observations"]["flow-ultra-01"]["container_up"])
        self.assertEqual(body["running_jobs"], [])


class EntrypointRequireProxyTests(unittest.TestCase):
    """The FU_REQUIRE_PROXY=1 check in docker/flow-ultra/entrypoint.sh, run against sample site.json files."""

    def _check(self, content):
        text = (ROOT / "docker/flow-ultra/entrypoint.sh").read_text()
        code = re.search(r"python3 - <<'SITE'.*?\n(.*?)\nSITE\n", text, re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "site.json")
            if content is not None:
                with open(path, "w") as f:
                    f.write(content)
            p = subprocess.run([sys.executable, "-c", code.replace("/opt/ext/site.json", path)], capture_output=True, timeout=20)
        return p.returncode

    def test_missing_or_invalid_site_json_refuses(self):
        self.assertEqual(self._check(json.dumps({"proxyUrl": "http://u:p@disp.oxylabs.io:8011", "proxyAllHosts": True})), 0)
        self.assertNotEqual(self._check(None), 0)
        self.assertNotEqual(self._check("{not json"), 0)
        self.assertNotEqual(self._check(json.dumps({"proxyUrl": "http://disp.oxylabs.io:8011", "proxyAllHosts": True})), 0)
        self.assertNotEqual(self._check(json.dumps({"proxyUrl": "http://u:p@disp.oxylabs.io:8011"})), 0)


if __name__ == "__main__":
    unittest.main()
