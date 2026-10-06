#!/usr/bin/env python3
"""gsync: copy ALL your repos from one git site to another (Gitea / GitHub).

  gsync.py                 # wizard: step 1 source site, step 2 destination, push all
  gsync.py sites           # list saved sites
  gsync.py forget NAME     # delete a saved site (NAME as shown by `sites`)

Flags for the wizard: --yes (no confirmation), --keep-visibility,
--force (push even if sensitive-looking files are found),
--overwrite NAME[,NAME] (force-push those repos, replacing destination history),
--verify-only (push nothing; compare every branch and tag on both sites).
"""
import argparse, base64, getpass, json, os, re, subprocess, sys, tempfile, time
import urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlparse

CONFIG = Path.home() / ".config" / "gsync" / "sites.json"
RISKY = re.compile(r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.pem|.*\.key)$")
NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------- credential storage ----------
def load_sites():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_sites(sites):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG.parent, 0o700)
    CONFIG.write_text(json.dumps(sites, indent=2))
    os.chmod(CONFIG, 0o600)  # owner read/write only


# ---------- site API (GitHub + Gitea) ----------
def api_base(site):
    return "https://api.github.com" if site["kind"] == "github" else f"{site['url']}/api/v1"


def call(site, method, path, body=None):
    req = urllib.request.Request(
        api_base(site) + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"token {site['token']}",
                 "Accept": "application/json",
                 "Content-Type": "application/json",
                 "User-Agent": "gsync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}
        body["_retry_after"] = e.headers.get("Retry-After")
        return e.code, body
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def find_bases(raw):
    """Return (candidate_base_urls, kind). Accepts a site link or a profile link and
    finds Gitea even under a sub-path (https://host/git/...). If the API demands a
    login even for /version (401/403), those paths are kept as candidates and
    checked later with your token."""
    raw = raw.strip()
    if not raw.startswith("http"):
        raw = "https://" + raw
    u = urlparse(raw)
    origin = f"{u.scheme}://{u.netloc}"
    if u.hostname in ("github.com", "www.github.com"):
        return ["https://github.com"], "github"
    parts = [p for p in u.path.split("/") if p]
    cands, notes = [], []
    for n in range(len(parts), -1, -1):  # /git/user, /git, host root
        base = origin + "".join("/" + p for p in parts[:n])
        req = urllib.request.Request(f"{base}/api/v1/version",
                                     headers={"User-Agent": "gsync"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if json.load(r).get("version"):
                    return [base], "gitea"
                notes.append(f"{base}: HTTP 200 but not Gitea JSON")
        except urllib.error.HTTPError as e:
            notes.append(f"{base}: HTTP {e.code}")
            if e.code in (401, 403):
                cands.append(base)
        except Exception as e:
            notes.append(f"{base}: {e}")
    if cands:
        print("  API asks for a login even for /version; will verify with your token")
        return cands, "gitea"
    print("  probe results:\n    " + "\n    ".join(notes))
    sys.exit("Could not find a Gitea API. Check the link, your network/VPN, and run:\n"
             f"  curl -i {origin}/git/api/v1/version")


def add_site(label, url=None):
    cands, kind = find_bases(url or input(f"{label} site link (e.g. https://github.com): "))
    token = getpass.getpass(f"Access token for {cands[0]} (hidden): ").strip()
    for base in cands:
        site = {"url": base, "kind": kind, "user": "", "token": token}
        code, data = call(site, "GET", "/user")
        if code == 200:
            break
        print(f"  {base}: login check returned HTTP {code}")
    else:
        sys.exit("Login failed on every candidate. Check the token and its scopes.")
    site["user"] = data.get("login") or data.get("username")
    sites = load_sites()
    sites[f"{site['user']}@{urlparse(base).netloc}"] = site
    save_sites(sites)
    print(f"  logged in as {site['user']} on {base} ({kind}); saved.")
    return site


def pick_site(label, wanted=None):
    sites = load_sites()
    if wanted:
        if wanted in sites:
            return sites[wanted]
        if "://" in wanted or "." in wanted:  # a link: only the token is asked
            return add_site(label, wanted)
        sys.exit(f"unknown site {wanted}; see ./gsync.py sites")
    if sites:
        keys = list(sites)
        for i, k in enumerate(keys, 1):
            print(f"  {i}) {k}")
        print("  n) new site")
        c = input(f"{label} [n]: ").strip().lower() or "n"
        if c.isdigit() and 1 <= int(c) <= len(keys):
            return sites[keys[int(c) - 1]]
    return add_site(label)


def list_repos(site):
    out, page = [], 1
    while True:
        q = (f"?per_page=100&page={page}&affiliation=owner" if site["kind"] == "github"
             else f"?limit=50&page={page}")
        code, data = call(site, "GET", "/user/repos" + q)
        if code != 200:
            sys.exit(f"Cannot list repos on {site['url']} (HTTP {code}); token scopes?")
        if not data:
            return out
        out += [r for r in data if r["owner"]["login"].lower() == site["user"].lower()]
        page += 1


class RateLimited(Exception):
    pass


def repo_exists(site, name):
    code, _ = call(site, "GET", f"/repos/{site['user']}/{name}")
    return code == 200


def create_repo(site, name, private, desc):
    if repo_exists(site, name):  # read-only call: no creation, no throttling
        return "already existed"
    for attempt in range(2):
        code, data = call(site, "POST", "/user/repos",
                          {"name": name, "private": private, "description": desc or ""})
        if code in (200, 201):
            time.sleep(5)  # pace creations; GitHub throttles bursts
            return "created"
        text = json.dumps(data).lower()
        msg = data.get("message", "")
        if code == 409 or (code == 422 and "already exist" in text):
            return "already existed"
        if code in (403, 429) and "rate limit" in text:
            try:
                wait = int(data.get("_retry_after") or 60)
            except ValueError:
                wait = 60
            if attempt == 0 and wait <= 120:
                print(f"    rate limited, waiting {wait}s once ...")
                time.sleep(wait)
                continue
            raise RateLimited(msg)
        raise RuntimeError(f"create repo failed (HTTP {code}): {msg or text[:150]}")
    raise RateLimited("still rate limited")


def check_dest(site):
    """Warn early if a GitHub token obviously can't create/push repos."""
    if site["kind"] != "github":
        return
    req = urllib.request.Request("https://api.github.com/user",
                                 headers={"Authorization": f"token {site['token']}",
                                          "User-Agent": "gsync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        scopes = r.headers.get("X-OAuth-Scopes")
    if scopes is None:
        print("  WARNING: this looks like a fine-grained token. It needs 'All repositories' "
              "access with Administration + Contents set to Read and write. "
              "A classic token with the 'repo' scope is simpler.")
    elif "repo" not in [s.strip() for s in scopes.split(",")]:
        sys.exit(f"  Token scopes are [{scopes or 'none'}] but 'repo' is required.\n"
                 "  Create a classic token with the 'repo' scope, then run again with "
                 "--dst https://github.com/YOURUSER to enter it.")
    else:
        print(f"  token scopes OK: {scopes}")


# ---------- git ----------
def git(args, cwd=None, site=None):
    """Credentials go in via environment, scoped to that site, never in argv/URL."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if site:
        b64 = base64.b64encode(f"{site['user']}:{site['token']}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0=f"http.{site['url']}/.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {b64}")
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def last(s):
    return (s.strip().splitlines() or ["unknown error"])[-1]


def ref_map(text):
    """Lines of '<hash> <ref>' -> {ref: hash} for branches and tags (peeled ^{} lines ignored)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if (len(parts) == 2 and parts[1].startswith(("refs/heads/", "refs/tags/"))
                and not parts[1].endswith("^{}")):
            out[parts[1]] = parts[0]
    return out


def verify(tmp, dst, dst_url):
    """Compare every branch and tag (name + commit hash) between the source clone and
    the destination. Identical hashes mean identical history and files."""
    local = git(["for-each-ref", "--format=%(objectname) %(refname)",
                 "refs/heads", "refs/tags"], cwd=tmp)
    remote = git(["ls-remote", "--heads", "--tags", dst_url], cwd=tmp, site=dst)
    if remote.returncode:
        return False, "could not read destination: " + last(remote.stderr)
    want, have = ref_map(local.stdout), ref_map(remote.stdout)
    missing = sorted(k for k in want if k not in have)
    differ = sorted(k for k in want if k in have and have[k] != want[k])
    if missing or differ:
        short = lambda refs: ", ".join(x.split("/", 2)[2] for x in refs[:4])
        parts = []
        if missing:
            parts.append(f"missing on destination: {short(missing)}")
        if differ:
            parts.append(f"different commits: {short(differ)}")
        return False, "; ".join(parts)
    heads = sum(1 for k in want if k.startswith("refs/heads/"))
    return True, f"{heads} branches, {len(want) - heads} tags identical"


def sync_one(r, src, dst, keep_vis, force, overwrite=(), verify_only=False):
    name = r["name"]
    if not NAME_OK.match(name):
        return "skipped", "unusual repo name"
    src_url = f"{src['url']}/{r['owner']['login']}/{name}.git"
    dst_url = f"{dst['url']}/{dst['user']}/{name}.git"
    with tempfile.TemporaryDirectory() as tmp:
        c = git(["clone", "--bare", src_url, tmp], site=src)
        if c.returncode:
            return "failed", "clone: " + last(c.stderr)
        tree = git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp)
        if tree.returncode:
            return "skipped", "empty repo"
        notes = []
        risky = [f for f in tree.stdout.splitlines() if RISKY.search(f)]
        if risky and not force and not verify_only:
            return "skipped", f"sensitive-looking files: {risky[:3]}"
        hist = git(["log", "--all", "--name-only", "--pretty=format:"], cwd=tmp)
        old = sorted({f for f in hist.stdout.splitlines() if RISKY.search(f)} - set(risky))
        if old:
            notes.append(f"WARNING: sensitive-looking files in older commits: {old[:3]}")
        attrs = git(["show", "HEAD:.gitattributes"], cwd=tmp)
        if "filter=lfs" in attrs.stdout:
            notes.append("WARNING: uses Git LFS, large files are NOT copied (only pointers)")

        if verify_only:
            if not repo_exists(dst, name):
                return "failed", "missing on destination"
            state = "checked"
        else:
            try:
                state = "repo " + create_repo(dst, name, r["private"] if keep_vis else True,
                                              r.get("description"))
            except RuntimeError as e:
                return "failed", str(e)
            for extra in (["--all"], ["--tags"]):
                flags = ["--force"] if name in overwrite else []
                p = git(["push", *flags, dst_url, *extra], cwd=tmp, site=dst)
                if p.returncode:
                    full = p.stderr
                    if "rejected" in full or "fast-forward" in full or "fetch first" in full:
                        err = ("destination repo already has different history, so nothing was "
                               "changed. Look at it on the destination; if it is disposable "
                               f"re-run with --overwrite {name}, or rename that repo there first")
                    else:
                        err = last(full)
                        if "403" in err:
                            err += "  -> destination token can't write (needs 'repo' scope / Contents: write)"
                    return "failed", f"push {extra[0]}: " + err
        ok, msg = verify(tmp, dst, dst_url)
        if not ok:
            return "failed", "verify: " + msg
        return "ok", "; ".join([f"{state}; verified {msg}"] + notes)


# ---------- wizard ----------
def run(a):
    print("STEP 1: source site (where your repos are now)")
    src = pick_site("Source site", a.src)
    print("\nSTEP 2: destination site (where to push them)")
    dst = pick_site("Destination site", a.dst)
    check_dest(dst)
    if (src["url"], src["user"]) == (dst["url"], dst["user"]):
        sys.exit("Source and destination are the same account.")

    repos = list_repos(src)
    if not repos:
        return print("No repos found on the source.")
    print(f"\n{len(repos)} repos on {src['url']} (owner {src['user']}):")
    for r in repos:
        print(f"  {'[private]' if r['private'] else '[public] '} {r['name']}")
    vis = "same visibility as source" if a.keep_visibility else "all created PRIVATE"
    if not a.yes and not a.verify_only and input(f"\nPush all to {dst['url']} as {dst['user']} ({vis})? [y/N] ").lower() != "y":
        return print("Cancelled.")

    overwrite = set(filter(None, (a.overwrite or "").split(",")))
    if overwrite:
        print(f"  --overwrite: will FORCE-push (replacing destination history) for: {', '.join(sorted(overwrite))}")
    results = []
    try:
        for i, r in enumerate(repos, 1):
            status, msg = sync_one(r, src, dst, a.keep_visibility, a.force, overwrite, a.verify_only)
            print(f"[{i}/{len(repos)}] {r['name']}: {status} ({msg})")
            results.append(status)
    except RateLimited as e:
        left = [x["name"] for x in repos[len(results):]]
        print(f"\nSTOPPED: GitHub is rate-limiting repo creation ({e})")
        print("Wait 30-60 minutes WITHOUT retrying (retries can extend the block), then run "
              "the same command again. Finished repos are detected and not recreated.")
        print(f"Not done yet ({len(left)}): {', '.join(left)}")
        sys.exit(2)
    except KeyboardInterrupt:
        print(f"\nInterrupted after {len(results)} of {len(repos)} repos.")
        sys.exit(130)
    print(f"\nDone: {results.count('ok')} ok, {results.count('skipped')} skipped, "
          f"{results.count('failed')} failed")
    sys.exit(1 if "failed" in results else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("sites")
    f = sub.add_parser("forget")
    f.add_argument("name")
    for q in (p,):
        q.add_argument("--src", help="saved site (user@host) or a link")
        q.add_argument("--dst", help="saved site (user@host) or a link")
        q.add_argument("--yes", action="store_true")
        q.add_argument("--keep-visibility", action="store_true")
        q.add_argument("--force", action="store_true")
        q.add_argument("--verify-only", action="store_true",
                       help="push nothing; just compare branches/tags on both sites")
        q.add_argument("--overwrite", metavar="NAMES",
                       help="comma-separated repo names whose destination history may be replaced (force push)")
    a = p.parse_args()

    if a.cmd == "sites":
        for k, s in load_sites().items():
            print(f"{k}  ({s['kind']})")
    elif a.cmd == "forget":
        sites = load_sites()
        if sites.pop(a.name, None):
            save_sites(sites)
            print("removed")
        else:
            print("no such site")
    else:
        run(a)


if __name__ == "__main__":
    main()
